import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from chat.management.commands.reconcile_live import Command
from chat.models import Room


def fake_api_response(payload):
    """模擬 urllib.request.urlopen 的 context manager 回傳值"""
    resp = MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return resp


class FetchReadyPathsTests(TestCase):
    """fetch_ready_paths：觀眾數計數與分頁"""

    @patch('chat.management.commands.reconcile_live.urllib.request.urlopen')
    def test_counts_readers_excluding_hls_muxer(self, mock_urlopen):
        mock_urlopen.return_value = fake_api_response({
            'pageCount': 1,
            'items': [
                {
                    'name': 'room_live',
                    'ready': True,
                    'readers': [
                        {'type': 'webRTCSession', 'id': 'a'},
                        {'type': 'webRTCSession', 'id': 'b'},
                        {'type': 'hlsMuxer', 'id': 'c'},
                        {'type': 'rtmpConn', 'id': 'd'},
                    ],
                },
                {'name': 'room_no_viewer', 'ready': True, 'readers': []},
                {'name': 'room_offline', 'ready': False, 'readers': []},
            ],
        })
        result = Command().fetch_ready_paths()
        # hlsMuxer 不計入；未 ready 的 path 不出現在結果
        self.assertEqual(result, {'room_live': 3, 'room_no_viewer': 0})

    @patch('chat.management.commands.reconcile_live.urllib.request.urlopen')
    def test_merges_all_pages(self, mock_urlopen):
        mock_urlopen.side_effect = [
            fake_api_response({
                'pageCount': 2,
                'items': [{'name': 'room_a', 'ready': True,
                           'readers': [{'type': 'webRTCSession', 'id': 'a'}]}],
            }),
            fake_api_response({
                'pageCount': 2,
                'items': [{'name': 'room_b', 'ready': True, 'readers': []}],
            }),
        ]
        result = Command().fetch_ready_paths()
        self.assertEqual(result, {'room_a': 1, 'room_b': 0})

    @patch('chat.management.commands.reconcile_live.urllib.request.urlopen')
    def test_returns_none_when_api_unreachable(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('connection refused')
        self.assertIsNone(Command().fetch_ready_paths())


class ReconcileTests(TestCase):
    """reconcile：is_live 對帳與觀眾數廣播"""

    def setUp(self):
        owner = User.objects.create_user(username='owner', password='pw')
        self.room = Room.objects.create(name='myroom', owner=owner)

    def run_reconcile(self, ready_paths):
        cmd = Command()
        group_send = AsyncMock()
        channel_layer = MagicMock(group_send=group_send)
        with patch.object(Command, 'fetch_ready_paths', return_value=ready_paths), \
             patch('chat.management.commands.reconcile_live.get_channel_layer',
                   return_value=channel_layer):
            cmd.reconcile()
        return [call.args for call in group_send.await_args_list]

    def test_broadcasts_status_change_and_viewer_count(self):
        calls = self.run_reconcile({'myroom': 2})
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_live)
        self.assertIn(('myroom', {'type': 'stream_status', 'is_live': True}), calls)
        self.assertIn(('myroom', {'type': 'viewer_count', 'count': 2}), calls)

    def test_broadcasts_viewer_count_every_round_without_change(self):
        # is_live 已一致時仍廣播觀眾數（無條件廣播，新進房者才拿得到數字）
        self.room.is_live = True
        self.room.save(update_fields=['is_live'])
        calls = self.run_reconcile({'myroom': 5})
        self.assertEqual(calls, [('myroom', {'type': 'viewer_count', 'count': 5})])

    def test_no_viewer_count_broadcast_when_offline(self):
        self.room.is_live = True
        self.room.save(update_fields=['is_live'])
        calls = self.run_reconcile({})
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_live)
        self.assertEqual(calls, [('myroom', {'type': 'stream_status', 'is_live': False})])
