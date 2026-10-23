from datetime import datetime, timezone as dt_timezone
from unittest.mock import AsyncMock, patch

from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from chat.consumers import ChatConsumer
from chat.models import Message
from chat.routing import websocket_urlpatterns

IN_MEMORY_LAYER = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
class HeartbeatTests(SimpleTestCase):
    """WebSocket 心跳：ping 直接回 pong 給該 socket，不進 DB、不廣播。

    用 SimpleTestCase（禁止 DB 存取）順便驗證 ping 路徑不寫 DB。
    """

    class FakeUser:
        is_anonymous = False
        is_authenticated = True

        def __str__(self):
            return 'tester'

    async def connect(self):
        communicator = WebsocketCommunicator(ChatConsumer.as_asgi(), '/ws/chat/testroom/')
        communicator.scope['user'] = self.FakeUser()
        communicator.scope['url_route'] = {'kwargs': {'room_name': 'testroom'}}
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_ping_replies_pong(self):
        communicator = await self.connect()
        await communicator.send_json_to({'type': 'ping'})
        self.assertEqual(await communicator.receive_json_from(),
                         {'message_type': 'pong'})
        # pong 之後沒有其他訊息（未廣播、未回音給群組）
        self.assertTrue(await communicator.receive_nothing())
        await communicator.disconnect()


class ChatConsumerTests(TransactionTestCase):
    """ChatConsumer：連線認證、文字 / 位置訊息的存檔與廣播、直播事件轉送。

    consumer 在另一個 thread 存取 DB，需用 TransactionTestCase 才看得到彼此的資料。
    經由 URLRouter 連線，順便驗證 WebSocket routing。
    """

    def setUp(self):
        # 每個測試各自啟用 override，channels 才會換一個全新的 InMemoryChannelLayer，
        # 避免群組成員跨測試殘留
        layer_override = override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
        layer_override.enable()
        self.addCleanup(layer_override.disable)
        self.alice = User.objects.create_user(username='alice')
        self.bob = User.objects.create_user(username='bob')
        self.communicators = []

    def communicator(self, user, room='testroom'):
        communicator = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f'/ws/chat/{room}/')
        communicator.scope['user'] = user
        self.communicators.append(communicator)
        return communicator

    async def connect(self, user, room='testroom'):
        communicator = self.communicator(user, room)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def disconnect_all(self):
        for communicator in self.communicators:
            await communicator.disconnect()

    async def test_rejects_anonymous(self):
        connected, _ = await self.communicator(AnonymousUser()).connect()
        self.assertFalse(connected)
        # 未認證的連線不應加入群組
        self.assertFalse(get_channel_layer().groups.get('testroom'))

    async def test_route_rejects_non_word_room_name(self):
        # routing 只接受 \w+，與 Room.name 的 RegexValidator 對應
        with self.assertRaises(ValueError):
            await self.communicator(self.alice, room='bad-name').connect(timeout=0.1)

    async def test_text_message_is_saved_and_broadcast_to_room(self):
        sender = await self.connect(self.alice)
        viewer = await self.connect(self.bob)
        # 2026-01-01 05:30 UTC = 台北 13:30，即時訊息時間需與歷史訊息一樣用 TIME_ZONE
        with patch('django.utils.timezone.now',
                   return_value=datetime(2026, 1, 1, 5, 30, tzinfo=dt_timezone.utc)):
            await sender.send_json_to({'type': 'text', 'message': '<b>hello</b>'})
            expected = {
                'message_type': 'text',
                'user': 'alice',
                'now_time': '13:30',
                'message': '<b>hello</b>',
            }
            self.assertEqual(await sender.receive_json_from(), expected)
            self.assertEqual(await viewer.receive_json_from(), expected)
        await self.disconnect_all()

        message = await Message.objects.select_related('user').aget()
        self.assertEqual(message.user, self.alice)
        self.assertEqual(message.group_name, 'testroom')
        # 存原文，XSS 防禦交給輸出端
        self.assertEqual(message.message, '<b>hello</b>')
        self.assertEqual(message.message_type, Message.TEXT)

    async def test_message_without_type_is_text(self):
        communicator = await self.connect(self.alice)
        await communicator.send_json_to({'message': 'hi'})
        payload = await communicator.receive_json_from()
        self.assertEqual(payload['message_type'], 'text')
        self.assertEqual(payload['message'], 'hi')
        await self.disconnect_all()

    async def test_unknown_type_falls_back_to_text(self):
        communicator = await self.connect(self.alice)
        for msg_type in ['emoji', 'zzz']:
            await communicator.send_json_to({'type': msg_type, 'message': 'hi'})
            payload = await communicator.receive_json_from()
            self.assertEqual((payload['message_type'], payload['message']), ('text', 'hi'))
        await self.disconnect_all()

    async def test_empty_message_is_ignored(self):
        communicator = await self.connect(self.alice)
        await communicator.send_json_to({'type': 'text', 'message': ''})
        await communicator.send_json_to({'type': 'text'})
        self.assertTrue(await communicator.receive_nothing())
        await self.disconnect_all()
        self.assertEqual(await Message.objects.acount(), 0)

    async def test_messages_stay_in_their_room(self):
        sender = await self.connect(self.alice, room='room_a')
        other = await self.connect(self.bob, room='room_b')
        await sender.send_json_to({'type': 'text', 'message': 'hi'})
        await sender.receive_json_from()
        self.assertTrue(await other.receive_nothing())
        await self.disconnect_all()

    async def test_location_is_saved_and_broadcast(self):
        sender = await self.connect(self.alice)
        viewer = await self.connect(self.bob)
        # 數字字串也接受（float() 轉換）
        await sender.send_json_to({'type': 'location', 'latitude': '25.0330', 'longitude': 121.5654})
        payload = await viewer.receive_json_from()
        await self.disconnect_all()
        self.assertRegex(payload.pop('now_time'), r'^\d\d:\d\d$')
        self.assertEqual(payload, {
            'message_type': 'location',
            'user': 'alice',
            'latitude': 25.033,
            'longitude': 121.5654,
        })

        message = await Message.objects.select_related('user').aget()
        self.assertEqual(message.user, self.alice)
        self.assertEqual(message.group_name, 'testroom')
        self.assertEqual(message.message_type, Message.LOCATION)
        self.assertEqual(message.message, '')
        self.assertEqual((message.latitude, message.longitude), (25.033, 121.5654))

    async def test_location_range_boundaries_are_accepted(self):
        communicator = await self.connect(self.alice)
        for lat, lng in [(90, 180), (-90, -180)]:
            await communicator.send_json_to({'type': 'location', 'latitude': lat, 'longitude': lng})
            payload = await communicator.receive_json_from()
            self.assertEqual((payload['latitude'], payload['longitude']), (lat, lng))
        await self.disconnect_all()
        self.assertEqual(await Message.objects.acount(), 2)

    async def test_location_out_of_range_is_rejected(self):
        communicator = await self.connect(self.alice)
        for lat, lng in [(90.001, 0), (-90.001, 0), (0, 180.001), (0, -180.001)]:
            with self.assertLogs('chat.consumers', 'WARNING'):
                await communicator.send_json_to({'type': 'location', 'latitude': lat, 'longitude': lng})
                self.assertTrue(await communicator.receive_nothing())
        await self.disconnect_all()
        self.assertEqual(await Message.objects.acount(), 0)

    async def test_invalid_location_payload_is_rejected(self):
        communicator = await self.connect(self.alice)
        for payload in [
            {'latitude': 25.0},                       # KeyError
            {'latitude': None, 'longitude': 121.5},   # TypeError
            {'latitude': 'abc', 'longitude': 121.5},  # ValueError
        ]:
            with self.assertLogs('chat.consumers', 'WARNING'):
                await communicator.send_json_to({'type': 'location', **payload})
                await communicator.receive_nothing()
        # 壞掉的 payload 不能讓 consumer 掛掉：之後的心跳仍有回應
        await communicator.send_json_to({'type': 'ping'})
        self.assertEqual(await communicator.receive_json_from(), {'message_type': 'pong'})
        await self.disconnect_all()
        self.assertEqual(await Message.objects.acount(), 0)

    async def test_nothing_is_broadcast_when_saving_fails(self):
        # 存檔失敗就不廣播，避免即時訊息與歷史記錄不一致
        communicator = await self.connect(self.alice)
        with patch('chat.consumers.Message.objects.create', side_effect=Exception('db down')):
            for payload in [{'type': 'text', 'message': 'hi'},
                            {'type': 'location', 'latitude': 1, 'longitude': 2}]:
                with self.assertLogs('chat.consumers', 'ERROR') as logs:
                    await communicator.send_json_to(payload)
                    self.assertTrue(await communicator.receive_nothing())
                self.assertIn('db down', logs.output[0])
        await self.disconnect_all()

    async def test_broadcast_failure_keeps_connection_open(self):
        communicator = await self.connect(self.alice)
        with patch.object(get_channel_layer(), 'group_send',
                          AsyncMock(side_effect=Exception('redis down'))):
            for payload in [{'type': 'text', 'message': 'hi'},
                            {'type': 'location', 'latitude': 1, 'longitude': 2}]:
                with self.assertLogs('chat.consumers', 'ERROR') as logs:
                    await communicator.send_json_to(payload)
                    self.assertTrue(await communicator.receive_nothing())
                self.assertIn('redis down', logs.output[0])
            await communicator.send_json_to({'type': 'ping'})
            self.assertEqual(await communicator.receive_json_from(), {'message_type': 'pong'})
        await self.disconnect_all()
        self.assertEqual(await Message.objects.acount(), 2)

    async def test_forwards_stream_status(self):
        communicator = await self.connect(self.alice)
        await get_channel_layer().group_send('testroom', {'type': 'stream_status', 'is_live': True})
        self.assertEqual(await communicator.receive_json_from(),
                         {'message_type': 'stream_status', 'is_live': True})
        await self.disconnect_all()

    async def test_renders_location_event_from_redis(self):
        # 正式環境的事件經 Redis（msgpack）反序列化，字串是新物件、不是 interned 常數，
        # 判斷訊息類型必須比較值而非 identity
        communicator = await self.connect(self.alice)
        await get_channel_layer().group_send('testroom', {
            'type': 'chat_message',
            'message_type': ''.join(['loc', 'ation']),
            'user': 'bob',
            'now_time': '13:30',
            'latitude': 1.5,
            'longitude': 2.5,
        })
        self.assertEqual(await communicator.receive_json_from(), {
            'message_type': 'location', 'user': 'bob', 'now_time': '13:30',
            'latitude': 1.5, 'longitude': 2.5,
        })
        await self.disconnect_all()

    async def test_forwards_viewer_count(self):
        communicator = await self.connect(self.alice)
        await get_channel_layer().group_send('testroom', {'type': 'viewer_count', 'count': 3})
        self.assertEqual(await communicator.receive_json_from(),
                         {'message_type': 'viewer_count', 'count': 3})
        await self.disconnect_all()

    async def test_disconnect_leaves_group(self):
        await self.connect(self.alice)
        groups = get_channel_layer().groups
        self.assertEqual(len(groups['testroom']), 1)
        await self.disconnect_all()
        self.assertFalse(groups.get('testroom'))
