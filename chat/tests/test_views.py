import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.test import Client, TestCase
from django.utils import timezone

from chat.models import Message, Room
from chat.tests.helpers import concurrent_room_update

READ_TOKEN_MAX_AGE = 12 * 3600  # README：觀看 token 12 小時有效


class IndexViewTests(TestCase):
    """首頁：直播間清單（直播中優先、每頁 12 筆）"""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner')

    def create_rooms(self, count, **kwargs):
        now = timezone.now()
        rooms = []
        for i in range(count):
            room = Room.objects.create(name=f'room{i}', owner=self.owner, **kwargs)
            # created 是 auto_now_add，事後改寫才能控制排序
            Room.objects.filter(pk=room.pk).update(created=now - timedelta(minutes=count - i))
            rooms.append(room)
        return rooms

    def test_anonymous_can_view(self):
        response = self.client.get('/chat/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'chat/index.html')
        # 前端用 uid4 組出隨機的純聊天室房名
        self.assertRegex(response.context['uid4'], r'^[0-9a-f]{32}$')

    def test_navbar_shows_logged_in_user(self):
        # render 必須帶 request，模板才拿得到 request.user / csrf_token
        self.client.force_login(self.owner)
        self.assertContains(self.client.get('/chat/'), 'Hello owner')

    def test_live_rooms_first_then_newest(self):
        oldest, middle, newest = self.create_rooms(3)
        Room.objects.filter(pk=oldest.pk).update(is_live=True)
        response = self.client.get('/chat/')
        self.assertEqual([r.name for r in response.context['live_rooms']],
                         [oldest.name, newest.name, middle.name])

    def test_paginates_12_per_page(self):
        rooms = self.create_rooms(13)
        page1 = self.client.get('/chat/').context['live_rooms']
        page2 = self.client.get('/chat/', {'page': 2}).context['live_rooms']
        self.assertEqual(len(page1), 12)
        self.assertEqual([r.name for r in page2], [rooms[0].name])

    def test_room_list_avoids_n_plus_one(self):
        for i in range(3):
            Room.objects.create(name=f'r{i}', owner=User.objects.create_user(username=f'u{i}'))
        # count + 該頁資料，主播名稱靠 select_related 一起撈
        with self.assertNumQueries(2):
            response = self.client.get('/chat/')
        self.assertContains(response, '主播：u2')


class CreateRoomViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice')

    def test_requires_login(self):
        response = self.client.get('/chat/create/')
        self.assertRedirects(response, '/account/login/?next=/chat/create/')

    def test_get_shows_form(self):
        self.client.force_login(self.user)
        response = self.client.get('/chat/create/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'chat/create_room.html')
        # GET 拿到的是空白表單，不能一開頁就顯示驗證錯誤
        self.assertFalse(response.context['form'].is_bound)
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_post_creates_room_owned_by_user(self):
        other = User.objects.create_user(username='mallory')
        self.client.force_login(self.user)
        # 表單只開放 name，owner / stream_key 不能從 POST 指定
        response = self.client.post('/chat/create/', {
            'name': 'myroom', 'owner': other.pk, 'stream_key': 'chosen-by-attacker',
        })
        self.assertRedirects(response, '/chat/myroom/')
        room = Room.objects.get(name='myroom')
        self.assertEqual(room.owner, self.user)
        self.assertNotEqual(room.stream_key, 'chosen-by-attacker')

    def test_reserved_name_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post('/chat/create/', {'name': 'create'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Room.objects.exists())

    def test_invalid_name_shows_errors(self):
        self.client.force_login(self.user)
        response = self.client.post('/chat/create/', {'name': 'bad-name'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertFalse(Room.objects.exists())

    def create_existing(self, count):
        for i in range(count):
            Room.objects.create(name=f'existing{i}', owner=self.user)

    def test_allows_last_slot(self):
        self.create_existing(settings.MAX_LIVE_ROOMS_PER_USER - 1)
        self.client.force_login(self.user)
        response = self.client.post('/chat/create/', {'name': 'myroom'})
        self.assertRedirects(response, '/chat/myroom/')

    def test_rejects_when_at_limit(self):
        self.create_existing(settings.MAX_LIVE_ROOMS_PER_USER)
        self.client.force_login(self.user)
        response = self.client.post('/chat/create/', {'name': 'myroom'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['form'].non_field_errors(),
                         [f'每人最多只能建立 {settings.MAX_LIVE_ROOMS_PER_USER} 個直播間'])
        self.assertFalse(Room.objects.filter(name='myroom').exists())

    def test_rejects_when_over_limit(self):
        # 例如管理員從後台多開、或上限調降後，已超過上限的人也不能再建
        self.create_existing(settings.MAX_LIVE_ROOMS_PER_USER + 1)
        self.client.force_login(self.user)
        self.client.post('/chat/create/', {'name': 'myroom'})
        self.assertFalse(Room.objects.filter(name='myroom').exists())

    def test_limit_counts_only_own_rooms(self):
        other = User.objects.create_user(username='bob')
        for i in range(settings.MAX_LIVE_ROOMS_PER_USER):
            Room.objects.create(name=f'bobs{i}', owner=other)
        self.client.force_login(self.user)
        response = self.client.post('/chat/create/', {'name': 'myroom'})
        self.assertRedirects(response, '/chat/myroom/')


class OwnerActionTestMixin:
    def setUp(self):
        self.owner = User.objects.create_user(username='owner')
        self.other = User.objects.create_user(username='other')
        self.room = Room.objects.create(name='myroom', owner=self.owner)


class DeleteRoomViewTests(OwnerActionTestMixin, TestCase):
    url = '/chat/myroom/delete/'

    def setUp(self):
        super().setUp()
        Message.objects.create(user=self.other, group_name='myroom', message='old')
        Message.objects.create(user=self.other, group_name='otherroom', message='keep')

    def test_owner_deletes_room_and_its_messages(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url)
        self.assertRedirects(response, '/chat/')
        self.assertFalse(Room.objects.filter(name='myroom').exists())
        # 房名釋放後可被他人重建，舊訊息必須一併刪除
        self.assertFalse(Message.objects.filter(group_name='myroom').exists())
        self.assertTrue(Message.objects.filter(group_name='otherroom').exists())

    def test_non_owner_gets_404(self):
        self.client.force_login(self.other)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Room.objects.filter(name='myroom').exists())
        self.assertTrue(Message.objects.filter(group_name='myroom').exists())

    def test_only_the_named_room_is_affected(self):
        # 房主只有一間房時，對別的房名操作也不能刪到自己這間，更不能刪掉別人房名的訊息
        Room.objects.create(name='otherroom', owner=self.other)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post('/chat/otherroom/delete/').status_code, 404)
        self.assertEqual(self.client.post('/chat/nope/delete/').status_code, 404)
        self.assertTrue(Room.objects.filter(name='myroom').exists())
        self.assertTrue(Message.objects.filter(group_name='otherroom').exists())

    def test_requires_post(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertTrue(Room.objects.filter(name='myroom').exists())

    def test_requires_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, f'/account/login/?next={self.url}')
        self.assertTrue(Room.objects.filter(name='myroom').exists())

    def test_room_survives_if_message_cleanup_fails(self):
        # 同一個 transaction：訊息刪不掉時房間也不能刪（否則房名釋放後舊訊息外洩）
        self.client.force_login(self.owner)
        with patch('chat.views.Message.objects.filter') as mock_filter:
            mock_filter.return_value.delete.side_effect = RuntimeError('db error')
            with self.assertRaises(RuntimeError):
                self.client.post(self.url)
        self.assertTrue(Room.objects.filter(name='myroom').exists())


class RegenerateStreamKeyViewTests(OwnerActionTestMixin, TestCase):
    url = '/chat/myroom/regenerate_key/'

    def test_owner_gets_new_key(self):
        old_key = self.room.stream_key
        self.client.force_login(self.owner)
        response = self.client.post(self.url)
        self.assertRedirects(response, '/chat/myroom/')
        self.room.refresh_from_db()
        self.assertNotEqual(self.room.stream_key, old_key)
        self.assertRegex(self.room.stream_key, r'^[0-9a-f]{32}$')

    def test_non_owner_gets_404(self):
        old_key = self.room.stream_key
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.room.refresh_from_db()
        self.assertEqual(self.room.stream_key, old_key)

    def test_does_not_overwrite_concurrent_status_change(self):
        # 輪替金鑰時剛好開播：只寫 stream_key，不能把 is_live 蓋回舊值
        self.client.force_login(self.owner)
        with concurrent_room_update(is_live=True):
            self.client.post(self.url)
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_live)

    def test_only_the_named_room_is_affected(self):
        old_key = self.room.stream_key
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post('/chat/nope/regenerate_key/').status_code, 404)
        self.room.refresh_from_db()
        self.assertEqual(self.room.stream_key, old_key)

    def test_requires_post(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_requires_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, f'/account/login/?next={self.url}')


class RoomViewTests(TestCase):
    def setUp(self):
        # 房主判斷只能用 ==：觀眾 id 比房主小或大都不能被當成房主；
        # 房主用大 id，因為 id 很小時 `is` 比較也會碰巧成立（CPython 小整數快取）
        self.viewer = User.objects.create_user(username='viewer')
        self.owner = User.objects.create_user(username='owner', id=100_000)
        self.late_viewer = User.objects.create_user(username='late_viewer')
        self.room = Room.objects.create(name='myroom', owner=self.owner)

    def test_plain_chat_room(self):
        # 沒有註冊 Room 的房名維持純聊天室
        self.client.force_login(self.viewer)
        response = self.client.get('/chat/chat_abc/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'chat/room.html')
        self.assertEqual(response.context['room_name'], 'chat_abc')
        self.assertIsNone(response.context['live_room'])
        self.assertFalse(response.context['is_owner'])
        self.assertEqual(response.context['live_read_token'], '')
        self.assertContains(response, '公開聊天室')

    def test_history_is_latest_100_of_this_room_oldest_first(self):
        start = timezone.now() - timedelta(hours=1)
        for i in range(101):
            msg = Message.objects.create(user=self.viewer, group_name='myroom', message=f'm{i}')
            Message.objects.filter(pk=msg.pk).update(created=start + timedelta(seconds=i))
        Message.objects.create(user=self.viewer, group_name='otherroom', message='other')
        self.client.force_login(self.viewer)
        history = self.client.get('/chat/myroom/').context['chat_messages']
        self.assertEqual([m.message for m in history], [f'm{i}' for i in range(1, 101)])

    def test_anonymous_gets_no_read_token(self):
        response = self.client.get('/chat/myroom/')
        self.assertEqual(response.context['live_room'], self.room)
        self.assertEqual(response.context['live_read_token'], '')
        self.assertFalse(response.context['is_owner'])
        self.assertContains(response, '後觀看直播')
        self.assertNotContains(response, self.room.stream_key)

    def test_viewer_gets_token_but_not_stream_key(self):
        for viewer in [self.viewer, self.late_viewer]:
            with self.subTest(viewer=viewer.username):
                self.client.force_login(viewer)
                response = self.client.get('/chat/myroom/')
                self.assertNotEqual(response.context['live_read_token'], '')
                self.assertFalse(response.context['is_owner'])
                self.assertNotContains(response, self.room.stream_key)

    def test_owner_sees_stream_key(self):
        self.client.force_login(self.owner)
        response = self.client.get('/chat/myroom/')
        self.assertTrue(response.context['is_owner'])
        self.assertContains(response, self.room.stream_key)


class StreamAuthTests(TestCase):
    """/live/auth/：MediaMTX authHTTPAddress 委派認證"""

    def setUp(self):
        # MediaMTX 不會帶 CSRF token：用會檢查 CSRF 的 client，確保端點有 csrf_exempt
        self.mediamtx = Client(enforce_csrf_checks=True)
        self.owner = User.objects.create_user(username='owner')
        self.viewer = User.objects.create_user(username='viewer')
        self.room = Room.objects.create(name='myroom', owner=self.owner)
        Room.objects.create(name='otherroom', owner=self.owner)

    def auth(self, payload, secret=settings.LIVE_AUTH_SECRET):
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return self.mediamtx.post(f'/live/auth/?secret={secret}', data=body,
                                  content_type='application/json').status_code

    def publish(self, **fields):
        return self.auth({'action': 'publish', 'path': 'myroom', **fields})

    def read_token(self, room_name='myroom'):
        """登入者開房間頁時拿到的播放 token"""
        self.client.force_login(self.viewer)
        token = self.client.get(f'/chat/{room_name}/').context['live_read_token']
        self.client.logout()
        return token

    def read(self, token, path='myroom'):
        return self.auth({'action': 'read', 'path': path, 'query': f'token={token}'})

    # --- 共享 secret ---

    def test_rejects_missing_or_wrong_secret(self):
        payload = {'action': 'publish', 'path': 'myroom', 'password': self.room.stream_key}
        self.assertEqual(self.auth(payload), 200)
        # 沒帶正確 secret 一律 403，不能被拿來當金鑰驗證 oracle
        self.assertEqual(self.auth(payload, secret=''), 403)
        self.assertEqual(self.auth(payload, secret='wrong'), 403)
        response = self.mediamtx.post('/live/auth/', data=json.dumps(payload),
                                      content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_requires_post(self):
        response = self.mediamtx.get(f'/live/auth/?secret={settings.LIVE_AUTH_SECRET}')
        self.assertEqual(response.status_code, 405)

    def test_invalid_json_is_bad_request(self):
        self.assertEqual(self.auth(b'not json'), 400)
        self.assertEqual(self.auth(b'\x80 not utf-8'), 400)

    def test_non_object_json_is_bad_request(self):
        for body in ['[1, 2]', 'null', '"publish"']:
            self.assertEqual(self.auth(body), 400)

    # --- 推流（publish） ---

    def test_publish_with_password_field(self):
        # RTMP：query string 帳密會被 MediaMTX 解析進 password 欄位
        self.assertEqual(self.publish(password=self.room.stream_key), 200)

    def test_publish_with_query_pass(self):
        # WHIP：帳密原封放在 query 欄位
        self.assertEqual(self.publish(query=f'user=owner&pass={self.room.stream_key}'), 200)

    def test_publish_rejects_wrong_key(self):
        self.assertEqual(self.publish(password='wrong'), 401)
        self.assertEqual(self.publish(query='user=owner&pass=wrong'), 401)

    def test_publish_rejects_missing_key(self):
        self.assertEqual(self.publish(), 401)
        self.assertEqual(self.publish(password='', query=''), 401)

    def test_publish_requires_exact_key(self):
        # 用固定的值測，不依賴隨機金鑰的大小關係（字典序在金鑰之前 / 之後的都要擋）
        key = self.room.stream_key
        for password in [key[:-1], key[:1], '0', key.upper(), key + '0', 'z' * len(key)]:
            with self.subTest(password=password):
                self.assertEqual(self.publish(password=password), 401)

    def test_publish_rejects_key_of_another_room(self):
        other_key = Room.objects.get(name='otherroom').stream_key
        self.assertEqual(self.publish(password=other_key), 401)

    def test_publish_rejects_empty_key_even_if_room_key_is_empty(self):
        Room.objects.filter(pk=self.room.pk).update(stream_key='')
        self.assertEqual(self.publish(password=''), 401)

    def test_publish_to_unregistered_path_is_rejected(self):
        payload = {'action': 'publish', 'path': 'chat_free', 'password': self.room.stream_key}
        self.assertEqual(self.auth(payload), 401)
        self.assertEqual(self.auth({'action': 'publish'}), 401)

    def test_old_key_rejected_after_regeneration(self):
        old_key = self.room.stream_key
        self.client.force_login(self.owner)
        self.client.post('/chat/myroom/regenerate_key/')
        self.room.refresh_from_db()
        self.assertEqual(self.publish(password=old_key), 401)
        self.assertEqual(self.publish(password=self.room.stream_key), 200)

    # --- 播放（read） ---

    def test_read_with_token_from_room_page(self):
        self.assertEqual(self.read(self.read_token()), 200)

    def test_read_token_is_bound_to_room(self):
        # 兩個方向都要擋（只比對單向時，可能漏掉「字典序在後的房間」）
        self.assertEqual(self.read(self.read_token('otherroom'), path='myroom'), 401)
        self.assertEqual(self.read(self.read_token('myroom'), path='otherroom'), 401)

    def test_read_rejects_missing_or_tampered_token(self):
        self.assertEqual(self.auth({'action': 'read', 'path': 'myroom'}), 401)
        self.assertEqual(self.read(''), 401)
        self.assertEqual(self.read(self.read_token() + 'x'), 401)

    def test_read_rejects_token_from_other_signer(self):
        # 其他用途（不同 salt）簽出來的值不能拿來播放
        self.assertEqual(self.read(signing.TimestampSigner().sign('myroom')), 401)

    def test_read_token_expires_after_12_hours(self):
        now = 1_800_000_000  # 簽名時間戳只到秒，用整數才能精準測邊界
        with patch('django.core.signing.time.time', return_value=now):
            token = self.read_token()
        with patch('django.core.signing.time.time', return_value=now + READ_TOKEN_MAX_AGE):
            self.assertEqual(self.read(token), 200)
        with patch('django.core.signing.time.time', return_value=now + READ_TOKEN_MAX_AGE + 1):
            self.assertEqual(self.read(token), 401)

    # --- 錄影回放（playback）：與觀看直播相同，需要登入者的 token ---

    def test_playback_requires_read_token(self):
        def playback(token, path='myroom'):
            return self.auth({'action': 'playback', 'path': path, 'query': f'token={token}'})
        self.assertEqual(playback(self.read_token()), 200)
        self.assertEqual(playback(''), 401)
        self.assertEqual(playback(self.read_token('otherroom')), 401)
        self.assertEqual(self.auth({'action': 'playback', 'path': 'myroom'}), 401)

    # --- 白名單以外的動作 ---

    def test_unknown_actions_are_rejected(self):
        # api / metrics / pprof 應由 authHTTPExclude 排除；設定漏掉或 MediaMTX 新增動作時一律拒絕
        for action in ['api', 'metrics', 'pprof', 'record', '', None]:
            with self.subTest(action=action):
                self.assertEqual(self.auth({'action': action, 'path': 'myroom',
                                            'password': self.room.stream_key}), 401)
        self.assertEqual(self.auth({'path': 'myroom'}), 401)


class StreamHookTests(TestCase):
    """/live/hook/：MediaMTX runOnReady / runOnNotReady 開播、關播通知"""

    def setUp(self):
        # MediaMTX 不會帶 CSRF token：用會檢查 CSRF 的 client，確保端點有 csrf_exempt
        self.mediamtx = Client(enforce_csrf_checks=True)
        owner = User.objects.create_user(username='owner')
        self.room = Room.objects.create(name='myroom', owner=owner)
        self.group_send = AsyncMock()
        patcher = patch('chat.views.get_channel_layer',
                        return_value=MagicMock(group_send=self.group_send))
        patcher.start()
        self.addCleanup(patcher.stop)

    def hook(self, secret=settings.LIVE_HOOK_SECRET, **data):
        return self.mediamtx.post('/live/hook/', {'secret': secret, **data}).status_code

    def broadcasts(self):
        return [call.args for call in self.group_send.await_args_list]

    def test_ready_marks_live_and_broadcasts(self):
        self.assertEqual(self.hook(path='myroom', event='ready'), 200)
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_live)
        self.assertEqual(self.broadcasts(), [('myroom', {'type': 'stream_status', 'is_live': True})])

    def test_notready_marks_offline_and_broadcasts(self):
        Room.objects.filter(pk=self.room.pk).update(is_live=True)
        self.assertEqual(self.hook(path='myroom', event='notready'), 200)
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_live)
        self.assertEqual(self.broadcasts(), [('myroom', {'type': 'stream_status', 'is_live': False})])

    def test_does_not_overwrite_concurrent_key_rotation(self):
        # 開播通知與房主輪替金鑰同時發生：只寫 is_live，不能把舊金鑰蓋回去
        with concurrent_room_update(stream_key='rotated'):
            self.hook(path='myroom', event='ready')
        self.room.refresh_from_db()
        self.assertEqual(self.room.stream_key, 'rotated')
        self.assertTrue(self.room.is_live)

    def test_missing_or_unknown_event_marks_offline(self):
        for data in [{}, {'event': 'zzz'}]:
            Room.objects.filter(pk=self.room.pk).update(is_live=True)
            self.assertEqual(self.hook(path='myroom', **data), 200)
            self.room.refresh_from_db()
            self.assertFalse(self.room.is_live)

    def test_unregistered_path_is_ignored(self):
        self.assertEqual(self.hook(path='chat_free', event='ready'), 200)
        self.assertEqual(self.broadcasts(), [])

    def test_rejects_missing_or_wrong_secret(self):
        # 否則任何人都能偽造「開播」
        self.assertEqual(self.hook(secret='wrong', path='myroom', event='ready'), 403)
        response = self.mediamtx.post('/live/hook/', {'path': 'myroom', 'event': 'ready'})
        self.assertEqual(response.status_code, 403)
        # auth secret 不能拿來打 hook：兩組 secret 權限隔離
        self.assertEqual(self.hook(secret=settings.LIVE_AUTH_SECRET, path='myroom', event='ready'), 403)
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_live)
        self.assertEqual(self.broadcasts(), [])

    def test_requires_post(self):
        self.assertEqual(self.mediamtx.get('/live/hook/').status_code, 405)
