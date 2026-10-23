"""room.html / index.html 前端 JS 的瀏覽器測試（Playwright）。

需要 Playwright（1.48 以上）：pip install playwright && playwright install chromium，
沒安裝時整組 skip；可用 `python manage.py test --exclude-tag=browser` 略過。

測試完全離線、不需要 Redis / MediaMTX：
- CDN 資源在本機回應：jQuery 用 Django admin 內建的同版本（3.7.1），
  Leaflet / hls.js 換成會記錄呼叫的 stub，其餘（Bootstrap、Font Awesome、地圖圖磚）直接攔掉
- WebSocket 由 page.route_web_socket 扮演 /ws/chat/ 的 server，可精準控制訊息與斷線
- MediaMTX 的 WHEP / HLS 請求被攔截記錄
- 心跳（25 秒）與重連退避用 page.clock 快轉，不必真的等
"""
import json
import os
import re
import time
from pathlib import Path
from unittest import SkipTest
from urllib.parse import parse_qs, urlsplit

import django
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag

from chat.models import Message, Room

try:
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

JQUERY = Path(django.__file__).parent / 'contrib/admin/static/admin/js/vendor/jquery/jquery.min.js'

# Leaflet stub：記錄地圖、標記、精度圈的呼叫
LEAFLET_STUB = """
window.__leaflet = [];
(function () {
    function layer(kind, args) {
        window.__leaflet.push([kind].concat(args));
        return {addTo: function () { return this; }};
    }
    window.L = {
        map: function (el, opts) {
            window.__mapOptions = opts;
            var m = {setView: function (latlng, zoom) {
                window.__leaflet.push(['map', el.className, latlng, zoom]);
                return m;
            }};
            return m;
        },
        tileLayer: function (url, opts) { window.__tileOptions = opts; return layer('tileLayer', [url]); },
        marker: function (latlng) { return layer('marker', [latlng]); },
        circle: function (latlng, opts) { return layer('circle', [latlng, opts.radius]); },
    };
})();
"""

# hls.js stub：記錄 loadSource / destroy；window.__hlsSupported = false 可模擬不支援
HLS_STUB = """
window.__hls = [];
window.Hls = function () {
    this.loadSource = function (url) { window.__hls.push(['load', url]); };
    this.attachMedia = function () { window.__hls.push(['attach']); };
    this.destroy = function () { window.__hls.push(['destroy']); };
};
window.Hls.isSupported = function () { return window.__hlsSupported !== false; };
"""


def wait_until(page, predicate, timeout=5.0, message='condition'):
    """Python 端的條件輪詢（page.wait_for_timeout 讓 Playwright 有機會處理事件）"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f'timed out waiting for {message}')
        page.wait_for_timeout(10)


class FakeChatServer:
    """用 route_web_socket 扮演 /ws/chat/<room>/ 的 server。"""

    def __init__(self, page, offline=False):
        self.page = page
        self.connections = []  # 每次成功的連線（含重連）一個 WebSocketRoute
        self.received = []     # client 送來的 JSON
        self.auto_pong = True
        # offline：把連線轉給真正的 server（live server 不處理 WebSocket），
        # client 會收到 error / close 而不會 onopen，用來模擬連不上
        self.offline = offline

    def install(self):
        self.page.route_web_socket(re.compile(r'/ws/chat/\w+/$'), self._on_connect)

    def _on_connect(self, ws):
        if self.offline:
            ws.connect_to_server()
            return
        self.connections.append(ws)
        ws.on_message(lambda message: self._on_message(ws, message))

    def _on_message(self, ws, message):
        data = json.loads(message)
        self.received.append(data)
        if data.get('type') == 'ping' and self.auto_pong:
            ws.send(json.dumps({'message_type': 'pong'}))

    def send(self, payload):
        self.connections[-1].send(json.dumps(payload))

    def wait_connected(self, count=1):
        wait_until(self.page, lambda: len(self.connections) >= count, message=f'{count} connection(s)')

    def wait_received(self, count):
        wait_until(self.page, lambda: len(self.received) >= count, message=f'{count} message(s)')
        return self.received


@tag('browser')
class BrowserTestCase(StaticLiveServerTestCase):
    host = '127.0.0.1'

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise SkipTest('需要 Playwright：pip install playwright && playwright install chromium')
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except PlaywrightError as e:
            cls.playwright.stop()
            raise SkipTest(f'無法啟動 Chromium（playwright install chromium）：{e}')
        # Playwright sync API 會在主執行緒留著 event loop，Django 會因此擋下 ORM 呼叫；
        # 只在這組測試期間放行，結束後還原
        cls._async_unsafe = os.environ.get('DJANGO_ALLOW_ASYNC_UNSAFE')
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.browser.close()
        cls.playwright.stop()
        if cls._async_unsafe is None:
            os.environ.pop('DJANGO_ALLOW_ASYNC_UNSAFE', None)
        else:
            os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = cls._async_unsafe

    # 子類別可覆寫，例如授權 geolocation
    context_options = {}

    def setUp(self):
        self.context = self.browser.new_context(**self.context_options)
        self.addCleanup(self.context.close)
        self.mediamtx_requests = []
        self.whep_status = 500  # None：不回應，請求一直 pending（模擬連線中）
        self.context.route(re.compile(r'^https://'), self._serve_cdn)
        self.context.route(re.compile(r'^http://127\.0\.0\.1:(8888|8889)/'), self._serve_mediamtx)

    def _serve_cdn(self, route):
        url = route.request.url
        cors = {'Access-Control-Allow-Origin': '*'}
        if url.endswith('/jquery.min.js'):
            route.fulfill(path=JQUERY, content_type='application/javascript')
        elif url.endswith('/leaflet.min.js'):
            route.fulfill(body=LEAFLET_STUB, content_type='application/javascript', headers=cors)
        elif url.endswith('/hls.min.js'):
            route.fulfill(body=HLS_STUB, content_type='application/javascript', headers=cors)
        else:
            route.abort()

    def _serve_mediamtx(self, route):
        request = route.request
        cors = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type',
        }
        if request.method == 'OPTIONS':  # CORS preflight
            route.fulfill(status=204, headers=cors)
            return
        self.mediamtx_requests.append({
            'method': request.method,
            'url': request.url,
            'content_type': request.headers.get('content-type'),
            'body': request.post_data,
        })
        if self.whep_status is not None:
            route.fulfill(status=self.whep_status, headers=cors)

    def whep_requests(self):
        return [r for r in self.mediamtx_requests if r['url'].startswith('http://127.0.0.1:8889/')]

    def login(self, user):
        self.client.force_login(user)
        self.context.add_cookies([{
            'name': settings.SESSION_COOKIE_NAME,
            'value': self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            'url': self.live_server_url,
        }])

    def open_page(self, path, clock=False, chat_server=True, offline=False):
        page = self.context.new_page()
        self.page_errors = []
        self.console = []
        page.on('pageerror', lambda error: self.page_errors.append(error))
        page.on('console', lambda msg: self.console.append(msg.text))
        self.chat = FakeChatServer(page, offline=offline)
        if chat_server:
            self.chat.install()
        if clock:
            # 凍結假時鐘（只 install 的話時間仍會照真實速度流動），之後只靠 run_for 前進：
            # 開頁時的 onopen / onclose 都發生在凍結的時間點，計時從那裡算起
            # （jQuery ready callback 也要快轉才會執行）
            page.clock.install(time=0)
            page.clock.pause_at(1000)
        page.goto(self.live_server_url + path)
        self.addCleanup(lambda: self.assertEqual(self.page_errors, [], 'JS 執行期錯誤'))
        return page

    def logs(self, pattern):
        return [m for m in self.console if re.search(pattern, m)]


class ChatMessageTests(BrowserTestCase):
    """聊天訊息的送出與顯示"""

    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user(username='alice')
        self.bob = User.objects.create_user(username='bob')
        self.login(self.alice)

    def open_lobby(self, **kwargs):
        page = self.open_page('/chat/chat_lobby/', **kwargs)
        self.chat.wait_connected()
        return page

    def test_connects_to_room_websocket(self):
        page = self.open_lobby()
        port = urlsplit(self.live_server_url).port
        self.assertEqual(self.chat.connections[0].url, f'ws://127.0.0.1:{port}/ws/chat/chat_lobby/')
        # 開頁就能直接打字
        self.assertEqual(page.evaluate('document.activeElement.id'), 'btn-input')

    def test_enter_sends_text_message(self):
        page = self.open_lobby()
        page.fill('#btn-input', 'hello')
        page.press('#btn-input', 'Enter')
        # Enter 打出的換行字元不能混進訊息
        self.assertEqual(self.chat.wait_received(1), [{'type': 'text', 'message': 'hello'}])
        expect(page.locator('#btn-input')).to_have_value('')

    def test_shift_enter_inserts_newline(self):
        page = self.open_lobby()
        page.press('#btn-input', 'l')
        page.press('#btn-input', 'Shift+Enter')
        page.press('#btn-input', 'm')
        page.press('#btn-input', 'Enter')
        self.assertEqual(self.chat.wait_received(1), [{'type': 'text', 'message': 'l\nm'}])

    def test_send_button(self):
        page = self.open_lobby()
        page.fill('#btn-input', 'hi')
        page.click('#btn-chat')
        self.assertEqual(self.chat.wait_received(1), [{'type': 'text', 'message': 'hi'}])

    def test_blank_message_is_not_sent(self):
        page = self.open_lobby()
        page.press('#btn-input', 'Enter')
        page.fill('#btn-input', '  \n ')
        page.click('#btn-chat')
        page.fill('#btn-input', 'after')
        page.click('#btn-chat')
        # 只收到最後一則：前面的空白訊息沒送出
        self.assertEqual(self.chat.wait_received(1), [{'type': 'text', 'message': 'after'}])
        page.wait_for_timeout(100)
        self.assertEqual(len(self.chat.received), 1)

    def test_input_is_kept_while_disconnected(self):
        page = self.open_lobby(clock=True)
        self.chat.connections[0].close()
        wait_until(page, lambda: self.logs('Chat socket closed'), message='close')
        page.fill('#btn-input', 'pending')
        page.click('#btn-chat')
        # 斷線中不送、也不清空，使用者的訊息不會不見
        expect(page.locator('#btn-input')).to_have_value('pending')
        page.clock.run_for(1000)
        self.chat.wait_connected(2)
        page.click('#btn-chat')
        self.assertEqual(self.chat.wait_received(1), [{'type': 'text', 'message': 'pending'}])
        expect(page.locator('#btn-input')).to_have_value('')

    def test_incoming_message_is_rendered_as_text(self):
        page = self.open_lobby()
        expect(page.locator('#chatMessages')).to_contain_text('No messages yet.')
        payload = '<img src=x onerror="window.__xss = 1">'
        self.chat.send({'message_type': 'text', 'user': 'bob', 'now_time': '13:30', 'message': payload})
        message = page.locator('.message', has=page.locator('.message-bubble'))
        expect(message.locator('.message-bubble')).to_have_text(payload)
        expect(message.locator('.message-bubble')).to_have_attribute('class', 'message-bubble')
        expect(message.locator('.location-link')).to_have_count(0)
        expect(message.locator('.message-avatar')).to_have_text('B')
        expect(message.locator('.message-time')).to_have_text('13:30')
        expect(message).not_to_have_class(re.compile(r'\bown\b'))
        self.assertEqual(page.locator('.message-bubble img').count(), 0)
        self.assertIsNone(page.evaluate('window.__xss'))
        # 佔位文字移除；新訊息插在「正在輸入」之前
        expect(page.locator('#chatMessages')).not_to_contain_text('No messages yet.')
        self.assertEqual(page.evaluate("document.getElementById('chatMessages').lastElementChild.id"),
                         'typingIndicator')

    def test_own_message_is_marked(self):
        page = self.open_lobby()
        self.chat.send({'message_type': 'text', 'user': 'alice', 'now_time': '13:30', 'message': 'mine'})
        expect(page.locator('.message.own .message-bubble')).to_have_text('mine')

    def test_pong_is_not_rendered(self):
        page = self.open_lobby()
        self.chat.send({'message_type': 'pong'})
        self.chat.send({'message_type': 'text', 'user': 'bob', 'now_time': '13:30', 'message': 'hi'})
        expect(page.locator('.message-bubble')).to_have_count(1)

    def test_location_message(self):
        page = self.open_lobby()
        self.chat.send({'message_type': 'location', 'user': 'bob', 'now_time': '13:30',
                        'latitude': 25.03, 'longitude': 121.56})
        link = page.locator('.location-link')
        expect(link).to_have_attribute(
            'href', 'https://www.openstreetmap.org/?mlat=25.03&mlon=121.56#map=15/25.03/121.56')
        calls = page.evaluate('window.__leaflet')
        self.assertIn(['map', 'location-map', [25.03, 121.56], 15], calls)
        self.assertIn(['marker', [25.03, 121.56]], calls)
        self.assertIn(['tileLayer', 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'], calls)
        # 聊天泡泡裡的地圖是靜態預覽：不能攔截捲動、拖曳等操作
        self.assertEqual(page.evaluate('window.__mapOptions'), {
            'zoomControl': False, 'dragging': False, 'scrollWheelZoom': False, 'doubleClickZoom': False,
            'boxZoom': False, 'keyboard': False, 'touchZoom': False, 'attributionControl': True,
        })
        # OSM 圖磚使用規範：標示出處、最大縮放 19
        tile_options = page.evaluate('window.__tileOptions')
        self.assertEqual(tile_options['maxZoom'], 19)
        self.assertIn('https://www.openstreetmap.org/copyright', tile_options['attribution'])
        # 訊息沒有精度資訊，不畫精度圈
        self.assertFalse([c for c in calls if c[0] == 'circle'])

    def test_history(self):
        for i in range(30):
            Message.objects.create(user=self.bob, group_name='chat_lobby', message=f'old {i}')
        Message.objects.create(user=self.bob, group_name='chat_lobby', message='<b>bold</b>')
        Message.objects.create(user=self.alice, group_name='chat_lobby', message_type=Message.LOCATION,
                               latitude=25.03, longitude=121.56)
        page = self.open_lobby()
        expect(page.locator('.message-bubble', has_text='<b>bold</b>')).to_have_count(1)
        expect(page.locator('.message.own .location-map')).to_have_count(1)
        wait_until(page, lambda: page.evaluate('window.__leaflet.length') > 0, message='history map')
        self.assertIn(['map', 'location-map', [25.03, 121.56], 15], page.evaluate('window.__leaflet'))
        at_bottom = "(el => el.scrollTop + el.clientHeight >= el.scrollHeight - 1)(document.getElementById('chatMessages'))"
        # 開頁捲到最底；收到新訊息也要捲到最底
        self.assertTrue(page.evaluate(at_bottom))
        self.chat.send({'message_type': 'text', 'user': 'bob', 'now_time': '13:30', 'message': 'new'})
        expect(page.locator('.message-bubble', has_text='new')).to_have_count(1)
        self.assertTrue(page.evaluate(at_bottom))
        for i in range(10):
            self.chat.send({'message_type': 'stream_status', 'is_live': i % 2 == 0})
        expect(page.locator('.live-notice')).to_have_count(10)
        self.assertTrue(page.evaluate(at_bottom))


class GeolocationTests(BrowserTestCase):
    """分享位置（navigator.geolocation，需要 secure context：127.0.0.1 符合）"""

    context_options = {
        'geolocation': {'latitude': 25.03, 'longitude': 121.56},
        'permissions': ['geolocation'],
    }

    def setUp(self):
        super().setUp()
        self.login(User.objects.create_user(username='alice'))
        self.dialogs = []

    def open_lobby(self, accept=True, **kwargs):
        page = self.open_page('/chat/chat_lobby/', **kwargs)
        page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message),
                                          dialog.accept() if accept else dialog.dismiss()))
        self.chat.wait_connected()
        return page

    def test_share_location_after_confirm(self):
        page = self.open_lobby()
        page.click('#btn-loc')
        self.assertEqual(self.chat.wait_received(1),
                         [{'type': 'location', 'latitude': 25.03, 'longitude': 121.56}])
        self.assertEqual(self.dialogs, ['將分享您目前的位置給此聊天室成員，繼續？'])
        expect(page.locator('#btn-loc')).to_be_enabled()

    def test_cancel_sends_nothing(self):
        page = self.open_lobby(accept=False)
        page.click('#btn-loc')
        page.wait_for_timeout(300)
        self.assertEqual(self.chat.received, [])
        expect(page.locator('#btn-loc')).to_be_enabled()

    def test_alert_when_disconnected(self):
        page = self.open_lobby(clock=True)
        self.chat.connections[0].close()
        wait_until(page, lambda: self.logs('Chat socket closed'), message='close')
        page.click('#btn-loc')
        expect(page.locator('.location-alert')).to_have_text('連線中斷，請稍後再試。')
        self.assertEqual(self.chat.received, [])


class GeolocationDeniedTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.login(User.objects.create_user(username='alice'))

    def test_permission_denied_shows_alert(self):
        page = self.open_page('/chat/chat_lobby/', clock=True)
        page.on('dialog', lambda dialog: dialog.accept())
        self.chat.wait_connected()
        page.click('#btn-loc')
        alert = page.locator('.location-alert')
        expect(alert).to_have_text('無法取得位置：已拒絕授權。')
        expect(page.locator('#btn-loc')).to_be_enabled()
        # 提示 3 秒後淡出移除
        page.clock.run_for(3000 + 300 + 50)
        expect(alert).to_have_count(0)

    def test_button_hidden_without_geolocation(self):
        self.context.add_init_script('delete Navigator.prototype.geolocation;')
        page = self.open_page('/chat/chat_lobby/')
        expect(page.locator('#btn-loc')).to_be_hidden()
        page.evaluate("$('#btn-loc').click()")
        expect(page.locator('.location-alert')).to_have_text('您的瀏覽器不支援定位功能。')


class GeolocationFlowTests(BrowserTestCase):
    """用 stub 取代 navigator.geolocation，精準控制定位結果"""

    GEO_STUB = """
        window.__geo = null;
        Object.defineProperty(Navigator.prototype, 'geolocation', {configurable: true, get: () => ({
            getCurrentPosition: (ok, err, opts) => { window.__geo = {ok, err, opts}; },
        })});
    """

    def setUp(self):
        super().setUp()
        self.login(User.objects.create_user(username='alice'))
        self.context.add_init_script(self.GEO_STUB)
        self.page = self.open_page('/chat/chat_lobby/')
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.chat.wait_connected()

    def request_location(self):
        self.page.click('#btn-loc')
        wait_until(self.page, lambda: self.page.evaluate('window.__geo !== null'), message='geolocation request')

    def test_button_disabled_while_locating(self):
        self.request_location()
        # 定位中避免重複點擊
        expect(self.page.locator('#btn-loc')).to_be_disabled()
        self.assertEqual(self.page.evaluate('window.__geo.opts'),
                         {'enableHighAccuracy': True, 'timeout': 10000, 'maximumAge': 60000})
        self.page.evaluate('window.__geo.ok({coords: {latitude: 1.5, longitude: 2.5}})')
        expect(self.page.locator('#btn-loc')).to_be_enabled()
        self.assertEqual(self.chat.wait_received(1), [{'type': 'location', 'latitude': 1.5, 'longitude': 2.5}])

    def test_error_messages(self):
        for code, text in [(1, '已拒絕授權。'), (2, '裝置無法定位。'), (3, '逾時。'), (99, 'boom')]:
            with self.subTest(code=code):
                self.page.evaluate('window.__geo = null')
                self.request_location()
                self.page.evaluate(f"""window.__geo.err({{code: {code}, message: 'boom',
                    PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3}})""")
                expect(self.page.locator('.location-alert').first).to_have_text('無法取得位置：' + text)
                expect(self.page.locator('#btn-loc')).to_be_enabled()
        self.assertEqual(self.chat.received, [])


class LivePlayerTests(BrowserTestCase):
    """直播播放器：WebRTC（WHEP）預設、可切 HLS、跟著開播 / 關播事件切換"""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(username='owner')
        self.room = Room.objects.create(name='myroom', owner=self.owner)
        self.login(User.objects.create_user(username='viewer'))
        # 記錄 RTCPeerConnection.close()，確認切換 / 關播時有釋放 WebRTC 連線
        self.context.add_init_script("""
            window.__pcClosed = 0;
            window.__offers = 0;
            const close = RTCPeerConnection.prototype.close;
            RTCPeerConnection.prototype.close = function () { window.__pcClosed++; return close.apply(this, arguments); };
            const createOffer = RTCPeerConnection.prototype.createOffer;
            RTCPeerConnection.prototype.createOffer = function () { window.__offers++; return createOffer.apply(this, arguments); };
        """)

    def open_room(self, is_live=False, **kwargs):
        Room.objects.filter(pk=self.room.pk).update(is_live=is_live)
        page = self.open_page('/chat/myroom/', **kwargs)
        self.chat.wait_connected()
        return page

    def wait_whep(self, page, count=1):
        wait_until(page, lambda: len(self.whep_requests()) >= count, message=f'{count} WHEP request(s)')
        return self.whep_requests()

    def test_offline_room(self):
        page = self.open_room()
        expect(page.locator('#liveOffline')).to_have_text('主播尚未開播')
        expect(page.locator('#liveOffline')).to_be_visible()
        expect(page.locator('#liveBadge')).to_be_hidden()
        expect(page.locator('#liveViewers')).to_be_hidden()
        page.wait_for_timeout(200)
        self.assertEqual(self.whep_requests(), [])

    def test_live_room_plays_webrtc_with_valid_token(self):
        page = self.open_room(is_live=True)
        expect(page.locator('#liveBadge')).to_be_visible()
        [request] = self.wait_whep(page)
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(request['content_type'], 'application/sdp')
        # 只收不送：影像與聲音各一個 recvonly transceiver
        self.assertIn('m=video', request['body'])
        self.assertIn('m=audio', request['body'])
        self.assertEqual(request['body'].count('a=recvonly'), 2)
        # 不用 trickle ICE：offer 要等 ICE 蒐集完、帶著 candidate 送出
        self.assertIn('a=candidate', request['body'])
        url = urlsplit(request['url'])
        self.assertEqual(url.path, '/myroom/whep')
        # 播放器帶的 token 要能通過 /live/auth/ 的 read 驗證
        token = parse_qs(url.query)['token'][0]
        response = self.client.post(f'/live/auth/?secret={settings.LIVE_AUTH_SECRET}', content_type='application/json',
                                    data={'action': 'read', 'path': 'myroom', 'query': f'token={token}'})
        self.assertEqual(response.status_code, 200)
        # MediaMTX 回錯誤時提示改用 HLS
        expect(page.locator('#liveOffline')).to_have_text('WebRTC 連線失敗，可改試 HLS')
        expect(page.locator('#liveOffline')).to_be_visible()
        self.assertTrue(self.logs('WHEP request failed: 500'))

    def test_stream_status_toggles_player(self):
        self.whep_status = None  # WHEP 連線中
        page = self.open_room()
        self.chat.send({'message_type': 'stream_status', 'is_live': True})
        expect(page.locator('.live-notice')).to_have_text('🔴 主播開播了')
        expect(page.locator('#liveBadge')).to_be_visible()
        expect(page.locator('#liveViewers')).to_be_visible()
        expect(page.locator('#liveOffline')).to_be_hidden()
        self.wait_whep(page)
        self.chat.send({'message_type': 'viewer_count', 'count': 7})
        expect(page.locator('#liveViewerCount')).to_have_text('7')

        self.chat.send({'message_type': 'stream_status', 'is_live': False})
        expect(page.locator('.live-notice').nth(1)).to_have_text('直播已結束')
        expect(page.locator('#liveOffline')).to_have_text('主播尚未開播')
        expect(page.locator('#liveBadge')).to_be_hidden()
        expect(page.locator('#liveViewers')).to_be_hidden()
        self.assertEqual(page.evaluate('window.__pcClosed'), 1)
        # 下次開播時不能殘留上一場的觀眾數
        expect(page.locator('#liveViewerCount')).to_have_text('–')

    def test_repeated_ready_does_not_restart_player(self):
        page = self.open_room(is_live=True)
        self.wait_whep(page)
        self.chat.send({'message_type': 'stream_status', 'is_live': True})
        expect(page.locator('.live-notice')).to_have_count(1)
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate('window.__offers'), 1)
        self.assertEqual(page.evaluate('window.__pcClosed'), 0)

    def test_switch_to_hls_and_back(self):
        page = self.open_room(is_live=True)
        self.wait_whep(page)
        page.click('#btnHls')
        expect(page.locator('#btnHls')).to_have_class(re.compile(r'\bactive\b'))
        expect(page.locator('#btnWebrtc')).not_to_have_class(re.compile(r'\bactive\b'))
        wait_until(page, lambda: page.evaluate('window.__hls.length') >= 2, message='hls load')
        [load, attach] = page.evaluate('window.__hls')
        self.assertEqual(load[0], 'load')
        url = urlsplit(load[1])
        self.assertEqual((url.netloc, url.path), ('127.0.0.1:8888', '/myroom/index.m3u8'))
        self.assertTrue(parse_qs(url.query)['token'][0])
        self.assertEqual(attach, ['attach'])
        self.assertEqual(page.evaluate('window.__pcClosed'), 1)
        # 再點同一個不重建
        page.click('#btnHls')
        page.wait_for_timeout(100)
        self.assertEqual(len(page.evaluate('window.__hls')), 2)
        # 切回 WebRTC：HLS 播放器要銷毀，重新發 WHEP
        page.click('#btnWebrtc')
        self.wait_whep(page, 2)
        self.assertIn(['destroy'], page.evaluate('window.__hls'))

    def test_hls_unsupported(self):
        self.context.add_init_script('window.__hlsSupported = false;')
        page = self.open_room(is_live=True)
        page.click('#btnHls')
        expect(page.locator('#liveOffline')).to_have_text('此瀏覽器不支援 HLS 播放')

    def test_native_hls_fallback(self):
        # Safari 沒有 MSE 但能原生播 HLS：直接把 URL 交給 <video>
        self.context.add_init_script("""
            window.__hlsSupported = false;
            HTMLMediaElement.prototype.canPlayType = function (type) {
                return type === 'application/vnd.apple.mpegurl' ? 'maybe' : '';
            };
        """)
        page = self.open_room(is_live=True)
        page.click('#btnHls')
        src = "document.getElementById('livePlayer').getAttribute('src')"
        wait_until(page, lambda: page.evaluate(src), message='native hls src')
        url = urlsplit(page.evaluate(src))
        self.assertEqual((url.netloc, url.path), ('127.0.0.1:8888', '/myroom/index.m3u8'))
        self.assertTrue(parse_qs(url.query)['token'][0])
        page.click('#btnWebrtc')
        wait_until(page, lambda: page.evaluate(src) is None, message='src removed')

    def test_protocol_choice_applies_when_stream_starts(self):
        page = self.open_room()
        page.click('#btnHls')
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate('window.__hls'), [])
        self.chat.send({'message_type': 'stream_status', 'is_live': True})
        wait_until(page, lambda: page.evaluate('window.__hls.length') >= 1, message='hls load')
        self.assertEqual(self.whep_requests(), [])

    def test_anonymous_sees_login_prompt(self):
        self.context.clear_cookies()
        page = self.open_page('/chat/myroom/')
        expect(page.locator('.live-offline a')).to_have_attribute('href', '/account/login/?next=/chat/myroom/')
        expect(page.locator('#livePlayer')).to_have_count(0)
        expect(page.locator('#chatMessages')).to_contain_text('Please Login to see messages.')
        # 未登入不載入播放器（也就拿不到 token）
        self.chat.wait_connected()
        self.chat.send({'message_type': 'stream_status', 'is_live': True})
        self.chat.send({'message_type': 'viewer_count', 'count': 3})
        expect(page.locator('.live-notice')).to_have_count(1)
        page.wait_for_timeout(200)
        self.assertEqual(self.mediamtx_requests, [])


class OwnerPanelTests(BrowserTestCase):
    """房主推流資訊面板"""

    def setUp(self):
        super().setUp()
        # 帳號含 + / @，驗證推流帳密有 urlencode
        self.owner = User.objects.create_user(username='host+1@x')
        self.room = Room.objects.create(name='myroom', owner=self.owner)

    def open_as_owner(self, accept=True):
        self.login(self.owner)
        page = self.open_page('/chat/myroom/')
        self.dialogs = []
        page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message),
                                          dialog.accept() if accept else dialog.dismiss()))
        return page

    def test_shows_stream_credentials(self):
        page = self.open_as_owner()
        key = self.room.stream_key
        expect(page.locator('#rtmpServer')).to_have_text('rtmp://127.0.0.1:1935')
        expect(page.locator('#rtmpKey')).to_have_text(f'myroom?user=host%2B1%40x&pass={key}')
        expect(page.locator('#whipPublish')).to_have_attribute(
            'href', f'http://127.0.0.1:8889/myroom/publish?user=host%2B1%40x&pass={key}')

    def test_viewer_has_no_owner_panel(self):
        self.login(User.objects.create_user(username='viewer'))
        page = self.open_page('/chat/myroom/')
        expect(page.locator('#livePlayer')).to_have_count(1)
        expect(page.locator('.live-owner-panel')).to_have_count(0)

    def test_regenerate_key_requires_confirmation(self):
        old_key = self.room.stream_key
        page = self.open_as_owner(accept=False)
        page.click('text=重新產生金鑰')
        page.wait_for_timeout(300)
        self.assertEqual(self.dialogs, ['確定要重新產生金鑰？舊金鑰將立即失效，OBS / WHIP 需更新設定。'])
        self.room.refresh_from_db()
        self.assertEqual(self.room.stream_key, old_key)

    def test_regenerate_key(self):
        old_key = self.room.stream_key
        page = self.open_as_owner()
        with page.expect_navigation():
            page.click('text=重新產生金鑰')
        self.room.refresh_from_db()
        self.assertNotEqual(self.room.stream_key, old_key)
        expect(page.locator('#rtmpKey')).to_contain_text(f'pass={self.room.stream_key}')

    def test_delete_room_requires_confirmation(self):
        page = self.open_as_owner(accept=False)
        page.click('text=刪除直播間')
        page.wait_for_timeout(300)
        self.assertEqual(self.dialogs, ['確定要刪除直播間？此動作無法復原，聊天記錄將一併刪除。'])
        self.assertTrue(Room.objects.filter(pk=self.room.pk).exists())

    def test_delete_room(self):
        page = self.open_as_owner()
        with page.expect_navigation():
            page.click('text=刪除直播間')
        self.assertEqual(urlsplit(page.url).path, '/chat/')
        self.assertFalse(Room.objects.filter(pk=self.room.pk).exists())


class HeartbeatTests(BrowserTestCase):
    """應用層心跳：每 25 秒 ping，10 秒內沒收到任何訊息就斷線重連（用假時鐘快轉）"""

    def setUp(self):
        super().setUp()
        self.login(User.objects.create_user(username='alice'))
        self.page = self.open_page('/chat/chat_lobby/', clock=True)
        self.chat.wait_connected()
        # onopen 才啟動心跳，等它跑完再快轉
        wait_until(self.page, lambda: self.logs('Chat socket connected'), message='onopen')

    def pings(self):
        return [m for m in self.chat.received if m == {'type': 'ping'}]

    def test_ping_every_25_seconds(self):
        self.page.clock.run_for(25_000 - 1)
        self.page.wait_for_timeout(100)
        self.assertEqual(self.pings(), [])
        self.page.clock.run_for(1)
        wait_until(self.page, lambda: len(self.pings()) == 1, message='first ping')
        self.page.clock.run_for(25_000)
        wait_until(self.page, lambda: len(self.pings()) == 2, message='second ping')

    def test_pong_keeps_connection(self):
        self.page.clock.run_for(25_000)
        wait_until(self.page, lambda: self.pings(), message='ping')
        self.page.wait_for_timeout(100)  # 讓 pong 送達
        self.page.clock.run_for(20_000)
        self.page.wait_for_timeout(100)
        self.assertEqual(len(self.chat.connections), 1)
        self.assertEqual(self.logs('No pong received'), [])

    def test_missing_pong_reconnects(self):
        self.chat.auto_pong = False
        self.page.clock.run_for(25_000)
        wait_until(self.page, lambda: self.pings(), message='ping')
        self.page.clock.run_for(10_000 - 1)
        self.page.wait_for_timeout(100)
        self.assertEqual(self.logs('No pong received'), [])
        self.page.clock.run_for(1)
        wait_until(self.page, lambda: self.logs('Chat socket closed'), message='close')
        self.assertTrue(self.logs('No pong received'))
        self.page.clock.run_for(1000)
        self.chat.wait_connected(2)

    def test_watchdog_cancelled_when_connection_drops(self):
        # ping 送出後連線就斷了：pong 計時器要跟著取消。否則重連一直失敗、退避累計超過 10 秒時，
        # 舊計時器會去關掉重連中的 socket，多排出一條重連
        self.chat.auto_pong = False
        self.page.clock.run_for(25_000)
        wait_until(self.page, lambda: self.pings(), message='ping')
        self.chat.offline = True
        self.chat.connections[0].close()
        closes = lambda: len(self.logs('Chat socket closed'))
        wait_until(self.page, lambda: closes() == 1, message='close')
        for n, delay in enumerate([1000, 1500, 2250, 3375, 5062.5], start=2):
            self.page.clock.run_for(int(delay) + 1)
            wait_until(self.page, lambda: closes() >= n, message=f'close #{n}')
        self.page.wait_for_timeout(100)
        self.assertEqual(self.logs('No pong received'), [])
        self.assertEqual(closes(), 6)

    def test_any_message_proves_connection_alive(self):
        self.chat.auto_pong = False
        self.page.clock.run_for(25_000)
        wait_until(self.page, lambda: self.pings(), message='ping')
        self.chat.send({'message_type': 'text', 'user': 'bob', 'now_time': '13:30', 'message': 'hi'})
        expect(self.page.locator('.message-bubble')).to_have_count(1)
        self.page.clock.run_for(10_000)
        self.page.wait_for_timeout(100)
        self.assertEqual(self.logs('No pong received'), [])
        self.assertEqual(len(self.chat.connections), 1)


class ReconnectTests(BrowserTestCase):
    """斷線重連：指數退避（1 秒起、1.5 倍成長、最多 30 秒），連上後重置"""

    def setUp(self):
        super().setUp()
        self.login(User.objects.create_user(username='alice'))
        # 先讓連線一律失敗
        self.page = self.open_page('/chat/chat_lobby/', clock=True, offline=True)
        self.attempts = 1  # 開頁時的第一次連線

    def delays(self):
        return [float(m.group(1)) for m in
                (re.search(r'reconnecting in (\S+) ms', text) for text in self.console) if m]

    def step(self, expected_delay):
        """等到這次斷線排好重連，確認在 expected_delay 之前不會重連、到點才重連"""
        wait_until(self.page, lambda: len(self.delays()) >= self.attempts, message=f'close #{self.attempts}')
        self.assertEqual(self.delays()[-1], expected_delay)
        sockets_before = len(self.chat.connections)
        self.page.clock.run_for(int(expected_delay) - 1)
        self.page.wait_for_timeout(50)
        self.assertEqual(len(self.delays()), self.attempts, 'reconnected too early')
        self.page.clock.run_for(2)
        self.attempts += 1
        return sockets_before

    def test_backoff_grows_and_caps(self):
        expected = [1000, 1500, 2250, 3375, 5062.5, 7593.75, 11390.625,
                    17085.9375, 25628.90625, 30000, 30000]
        for delay in expected:
            self.step(delay)
        wait_until(self.page, lambda: len(self.delays()) >= self.attempts, message='last close')
        self.assertEqual(self.delays(), expected + [30000])

    def test_successful_connection_resets_backoff(self):
        for delay in [1000, 1500]:
            self.step(delay)
        wait_until(self.page, lambda: len(self.delays()) >= self.attempts, message='close')
        self.chat.offline = False  # 之後的連線會成功
        self.page.clock.run_for(2250)
        self.chat.wait_connected()
        self.chat.connections[0].close()
        wait_until(self.page, lambda: len(self.delays()) > self.attempts, message='close after success')
        self.assertEqual(self.delays()[-1], 1000)


class IndexPageTests(BrowserTestCase):
    def test_enter_room_name(self):
        page = self.open_page('/chat/', chat_server=False)
        self.assertEqual(page.evaluate('document.activeElement.id'), 'room-name-input')
        page.locator('#room-name-input').press_sequentially('lobby')
        with page.expect_navigation():
            page.press('#room-name-input', 'Enter')
        self.assertRegex(urlsplit(page.url).path, r'^/chat/chat_lobby_[0-9a-f]{32}/$')
        expect(page.locator('.chat-title')).to_have_text(re.compile(r'^chat_lobby_[0-9a-f]{32}$'))

    def test_enter_button(self):
        page = self.open_page('/chat/', chat_server=False)
        page.fill('#room-name-input', 'lobby')
        with page.expect_navigation():
            page.click('#room-name-submit')
        self.assertRegex(urlsplit(page.url).path, r'^/chat/chat_lobby_[0-9a-f]{32}/$')
