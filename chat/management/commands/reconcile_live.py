import json
import time
import urllib.request

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.management.base import BaseCommand

from chat.models import Room


class Command(BaseCommand):
    help = '對帳 Room.is_live 與 MediaMTX 實際狀態，修正不一致並廣播到聊天室'

    def add_arguments(self, parser):
        parser.add_argument('--loop', type=int, default=0,
                            help='每隔 N 秒重複執行（0 = 只跑一次）')

    def handle(self, *args, **options):
        interval = options['loop']
        while True:
            try:
                self.reconcile()
            except Exception as e:
                # 對帳失敗不中斷迴圈，下一輪再試
                self.stderr.write(f'reconcile failed: {e}')
            if not interval:
                break
            time.sleep(interval)

    def reconcile(self):
        ready_paths = self.fetch_ready_paths()
        if ready_paths is None:
            self.stderr.write('MediaMTX API 連不上，跳過本輪')
            return
        for room in Room.objects.all():
            actual = room.name in ready_paths
            if room.is_live == actual:
                continue
            room.is_live = actual
            room.save(update_fields=['is_live'])
            async_to_sync(get_channel_layer().group_send)(
                room.name,
                {'type': 'stream_status', 'is_live': actual},
            )
            self.stdout.write(f'{room.name}: is_live -> {actual}')

    def fetch_ready_paths(self):
        # 逐頁列出所有 path：API 一頁預設 100 筆，只讀第一頁
        # 會把排在後面的直播中房間誤判為離線
        ready = set()
        page = 0
        while True:
            url = f'{settings.MEDIAMTX_API_URL}/v3/paths/list?itemsPerPage=100&page={page}'
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.load(resp)
            except (OSError, ValueError):
                return None
            ready.update(item['name'] for item in data.get('items', []) if item.get('ready'))
            page += 1
            if page >= data.get('pageCount', 1):
                return ready
