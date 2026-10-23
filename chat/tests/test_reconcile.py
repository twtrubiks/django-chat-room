import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from chat.management.commands.reconcile_live import Command
from chat.models import Room
from chat.tests.helpers import concurrent_room_update

URLOPEN = 'chat.management.commands.reconcile_live.urllib.request.urlopen'


def fake_api_response(payload):
    """模擬 urllib.request.urlopen 的 context manager 回傳值"""
    resp = MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return resp


class FetchReadyPathsTests(TestCase):
    """fetch_ready_paths：觀眾數計數與分頁

    mock 一律用有限的 side_effect（而非 return_value）：翻頁邏輯壞掉時
    會因 StopIteration 立即失敗，而不是無窮迴圈卡到逾時。
    """

    @patch(URLOPEN)
    def test_counts_readers_excluding_hls_muxer(self, mock_urlopen):
        mock_urlopen.side_effect = [fake_api_response({
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
        })]
        result = Command().fetch_ready_paths()
        # hlsMuxer 不計入；未 ready 的 path 不出現在結果
        self.assertEqual(result, {'room_live': 3, 'room_no_viewer': 0})

    @patch(URLOPEN)
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

    @override_settings(MEDIAMTX_API_URL='http://mtx.test:9997')
    @patch(URLOPEN)
    def test_requests_every_page_from_configured_api(self, mock_urlopen):
        mock_urlopen.side_effect = [
            fake_api_response({'pageCount': 2, 'items': []}),
            fake_api_response({'pageCount': 2, 'items': []}),
        ]
        Command().fetch_ready_paths()
        self.assertEqual(
            [call.args[0] for call in mock_urlopen.call_args_list],
            ['http://mtx.test:9997/v3/paths/list?itemsPerPage=100&page=0',
             'http://mtx.test:9997/v3/paths/list?itemsPerPage=100&page=1'],
        )
        # API 卡住時不能讓 reconciler 永遠卡在 urlopen
        for call in mock_urlopen.call_args_list:
            self.assertIsNotNone(call.kwargs.get('timeout'))

    @patch(URLOPEN)
    def test_single_page_when_page_count_missing(self, mock_urlopen):
        # 缺 readers 也視為 0 位觀眾
        mock_urlopen.side_effect = [fake_api_response({
            'items': [{'name': 'room_a', 'ready': True}],
        })]
        self.assertEqual(Command().fetch_ready_paths(), {'room_a': 0})
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch(URLOPEN)
    def test_no_paths(self, mock_urlopen):
        # 沒有任何 path 時 MediaMTX 回傳 pageCount=0，不能一直往下翻頁；缺 items 也視為空
        for payload in [{'pageCount': 0, 'items': []}, {'pageCount': 0}]:
            mock_urlopen.reset_mock()
            mock_urlopen.side_effect = [fake_api_response(payload)]
            self.assertEqual(Command().fetch_ready_paths(), {})
            self.assertEqual(mock_urlopen.call_count, 1)

    @patch(URLOPEN)
    def test_returns_none_when_api_unreachable(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('connection refused')
        self.assertIsNone(Command().fetch_ready_paths())

    @patch(URLOPEN)
    def test_returns_none_on_invalid_json(self, mock_urlopen):
        resp = MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'<html>502 Bad Gateway</html>')
        mock_urlopen.side_effect = [resp]
        self.assertIsNone(Command().fetch_ready_paths())

    @patch(URLOPEN)
    def test_returns_none_when_later_page_fails(self, mock_urlopen):
        # 只拿到部分頁面時不能回傳殘缺結果，否則後面頁面的直播中房間會被誤判為離線
        mock_urlopen.side_effect = [
            fake_api_response({'pageCount': 2, 'items': [{'name': 'room_a', 'ready': True}]}),
            OSError('connection reset'),
        ]
        self.assertIsNone(Command().fetch_ready_paths())


class ReconcileTests(TestCase):
    """reconcile：is_live 對帳與觀眾數廣播"""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner')
        self.room = Room.objects.create(name='myroom', owner=self.owner)

    def run_reconcile(self, ready_paths):
        self.cmd = Command(stdout=io.StringIO(), stderr=io.StringIO())
        group_send = AsyncMock()
        channel_layer = MagicMock(group_send=group_send)
        with patch.object(Command, 'fetch_ready_paths', return_value=ready_paths), \
             patch('chat.management.commands.reconcile_live.get_channel_layer',
                   return_value=channel_layer):
            self.cmd.reconcile()
        return [call.args for call in group_send.await_args_list]

    def test_broadcasts_status_change_and_viewer_count(self):
        calls = self.run_reconcile({'myroom': 2})
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_live)
        self.assertIn(('myroom', {'type': 'stream_status', 'is_live': True}), calls)
        self.assertIn(('myroom', {'type': 'viewer_count', 'count': 2}), calls)
        self.assertIn('myroom: is_live -> True', self.cmd.stdout.getvalue())

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

    def test_does_not_overwrite_concurrent_key_rotation(self):
        # 對帳途中房主輪替金鑰：只寫 is_live，不能把舊金鑰蓋回去
        with concurrent_room_update(stream_key='rotated'):
            self.run_reconcile({'myroom': 1})
        self.room.refresh_from_db()
        self.assertEqual(self.room.stream_key, 'rotated')
        self.assertTrue(self.room.is_live)

    def test_consistent_offline_room_is_left_alone(self):
        calls = self.run_reconcile({'other_path': 1})
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_live)
        self.assertEqual(calls, [])
        self.assertEqual(self.cmd.stdout.getvalue(), '')

    def test_reconciles_each_room_independently(self):
        Room.objects.create(name='second', owner=self.owner, is_live=True)
        calls = self.run_reconcile({'myroom': 1})
        self.assertEqual(
            dict(Room.objects.values_list('name', 'is_live')),
            {'myroom': True, 'second': False},
        )
        self.assertIn(('second', {'type': 'stream_status', 'is_live': False}), calls)
        self.assertIn(('myroom', {'type': 'stream_status', 'is_live': True}), calls)

    def test_skips_round_when_api_unreachable(self):
        # API 連不上時不能把所有房間當成離線
        self.room.is_live = True
        self.room.save(update_fields=['is_live'])
        calls = self.run_reconcile(None)
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_live)
        self.assertEqual(calls, [])
        self.assertIn('跳過本輪', self.cmd.stderr.getvalue())


class StopLoop(Exception):
    """用來跳出 --loop 的無窮迴圈"""


class HandleTests(TestCase):
    """handle：單次執行與 --loop 迴圈"""

    def setUp(self):
        # sleep 預設一被呼叫就丟例外：誤入迴圈時立即失敗，而不是卡在無窮迴圈
        patcher = patch('chat.management.commands.reconcile_live.time.sleep', side_effect=StopLoop)
        self.mock_sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, *args):
        stderr = io.StringIO()
        call_command('reconcile_live', *args, stdout=io.StringIO(), stderr=stderr)
        return stderr.getvalue()

    @patch.object(Command, 'reconcile')
    def test_runs_once_by_default(self, mock_reconcile):
        self.call()
        mock_reconcile.assert_called_once_with()
        self.mock_sleep.assert_not_called()

    @patch.object(Command, 'reconcile')
    def test_loop_sleeps_interval_between_rounds(self, mock_reconcile):
        self.mock_sleep.side_effect = [None, StopLoop]
        with self.assertRaises(StopLoop):
            self.call('--loop', '30')
        self.assertEqual(mock_reconcile.call_count, 2)
        self.assertEqual([call.args for call in self.mock_sleep.call_args_list], [(30,), (30,)])

    @patch.object(Command, 'reconcile', side_effect=[RuntimeError('boom'), None])
    def test_failed_round_does_not_stop_loop(self, mock_reconcile):
        self.mock_sleep.side_effect = [None, StopLoop]
        with self.assertRaises(StopLoop):
            self.call('--loop', '30')
        self.assertEqual(mock_reconcile.call_count, 2)

    @patch.object(Command, 'reconcile', side_effect=RuntimeError('boom'))
    def test_failed_round_is_reported(self, mock_reconcile):
        self.assertIn('reconcile failed: boom', self.call())
