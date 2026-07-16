import json
import urllib.error
import urllib.request
import uuid
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Message, Room


# Create your views here.

def index(request):
    uid4 = uuid.uuid4().hex
    return render(request, 'chat/index.html', {'uid4': uid4})


def _query_mediamtx_is_live(name):
    """
    向 MediaMTX API 查詢串流實際狀態，回傳 True / False；
    API 連不上（狀態未知）回 None。
    """
    url = f'{settings.MEDIAMTX_API_URL}/v3/paths/get/{name}'
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.load(resp).get('ready', False)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False  # path 不存在 = 未開播
        return None
    except (OSError, ValueError):
        return None


def room(request, room_name):
    chat_messages = Message.objects.filter(group_name=room_name).order_by("created")[:100]
    # 有註冊 Room 的房名才有直播功能，否則維持純聊天室
    live_room = Room.objects.filter(name=room_name).first()
    if live_room is not None:
        # 開頁時與 MediaMTX 實際狀態對帳：webhook 是一次性通知，
        # 若遺失（如 MediaMTX 崩潰）is_live 會永遠卡住，靠這裡收斂
        actual_is_live = _query_mediamtx_is_live(live_room.name)
        if actual_is_live is not None and actual_is_live != live_room.is_live:
            live_room.is_live = actual_is_live
            live_room.save(update_fields=['is_live'])
    is_owner = (
        live_room is not None
        and request.user.is_authenticated
        and live_room.owner_id == request.user.id
    )
    return render(request, 'chat/room.html', {
        'chat_messages': chat_messages,
        'room_name': room_name,
        'live_room': live_room,
        'is_owner': is_owner,
    })


@csrf_exempt
@require_POST
def stream_auth(request):
    """
    MediaMTX authHTTPAddress 委派認證端點，回應 20x 放行、其他拒絕。
    RTMP 推流帳密要用 query string（?user=&pass=），不能用 URL userinfo。
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    # 只限制推流，播放不設限
    if data.get('action') != 'publish':
        return HttpResponse(status=200)

    # RTMP 的 query string 帳密會被 MediaMTX 解析進 password 欄位，
    # 但 WHIP（WebRTC 推流）只會原封放在 query 欄位（實測），需自行解析；
    # WHIP 走 HTTP Basic auth 時則會填入 password 欄位
    password = data.get('password', '')
    if not password:
        query = parse_qs(data.get('query', ''))
        password = (query.get('pass') or [''])[0]

    room = Room.objects.filter(name=data.get('path', '')).first()
    if room and password and password == room.stream_key:
        return HttpResponse(status=200)
    return HttpResponse(status=401)


@csrf_exempt
@require_POST
def stream_hook(request):
    """
    MediaMTX runOnReady / runOnNotReady 開播、關播通知，
    更新 Room.is_live 並廣播到聊天室。以共享 secret 驗證來源。
    """
    if request.POST.get('secret') != settings.LIVE_HOOK_SECRET:
        return HttpResponse(status=403)

    room = Room.objects.filter(name=request.POST.get('path', '')).first()
    if room is None:
        # 未註冊 Room 的房名沒有直播功能，忽略
        return HttpResponse(status=200)

    room.is_live = request.POST.get('event') == 'ready'
    room.save(update_fields=['is_live'])

    async_to_sync(get_channel_layer().group_send)(
        room.name,
        {'type': 'stream_status', 'is_live': room.is_live},
    )
    return HttpResponse(status=200)
