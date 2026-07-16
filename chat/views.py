import json
import uuid
from urllib.parse import parse_qs

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import RoomCreateForm
from .models import Message, Room, generate_stream_key

# 觀看直播的簽名 token：綁定房名、12 小時內有效
_read_token_signer = signing.TimestampSigner(salt='live-read')
READ_TOKEN_MAX_AGE = 12 * 3600


# Create your views here.

def index(request):
    uid4 = uuid.uuid4().hex
    rooms_qs = Room.objects.select_related('owner').order_by('-is_live', '-created')
    live_rooms = Paginator(rooms_qs, 12).get_page(request.GET.get('page'))
    return render(request, 'chat/index.html', {
        'uid4': uid4,
        'live_rooms': live_rooms,
    })


@login_required
def create_room(request):
    if request.method == 'POST':
        form = RoomCreateForm(request.POST)
        if form.is_valid():
            if request.user.rooms.count() >= settings.MAX_LIVE_ROOMS_PER_USER:
                form.add_error(None, f'每人最多只能建立 {settings.MAX_LIVE_ROOMS_PER_USER} 個直播間')
            else:
                live_room = form.save(commit=False)
                live_room.owner = request.user
                live_room.save()
                return redirect('room', room_name=live_room.name)
    else:
        form = RoomCreateForm()
    return render(request, 'chat/create_room.html', {'form': form})


@login_required
@require_POST
def delete_room(request, room_name):
    """刪除直播間（聊天記錄以 group_name 弱關聯，不隨之刪除）"""
    live_room = Room.objects.filter(name=room_name, owner=request.user).first()
    if live_room is None:
        return HttpResponse(status=404)
    live_room.delete()
    return redirect('chat')


@login_required
@require_POST
def regenerate_stream_key(request, room_name):
    """金鑰外洩時房主可自行輪替，舊金鑰立即失效（下次推流生效）"""
    live_room = Room.objects.filter(name=room_name, owner=request.user).first()
    if live_room is None:
        return HttpResponse(status=404)
    live_room.stream_key = generate_stream_key()
    live_room.save(update_fields=['stream_key'])
    return redirect('room', room_name=room_name)


def room(request, room_name):
    chat_messages = Message.objects.filter(group_name=room_name).order_by("created")[:100]
    # 有註冊 Room 的房名才有直播功能，否則維持純聊天室。
    # is_live 的對帳只由 reconciler 服務負責（30 秒收斂並廣播）：
    # 在這裡同步對帳除了阻塞頁面，還會靜默改掉 DB 使 reconciler
    # 認為狀態一致而跳過廣播，已連線的觀眾就永遠收不到修正
    live_room = Room.objects.filter(name=room_name).first()
    is_owner = (
        live_room is not None
        and request.user.is_authenticated
        and live_room.owner_id == request.user.id
    )
    # 觀看直播需登入：登入者才拿得到播放用的簽名 token
    live_read_token = ''
    if live_room is not None and request.user.is_authenticated:
        live_read_token = _read_token_signer.sign(live_room.name)
    return render(request, 'chat/room.html', {
        'chat_messages': chat_messages,
        'room_name': room_name,
        'live_room': live_room,
        'is_owner': is_owner,
        'live_read_token': live_read_token,
    })


@csrf_exempt
@require_POST
def stream_auth(request):
    """
    MediaMTX authHTTPAddress 委派認證端點，回應 20x 放行、其他拒絕。
    RTMP 推流帳密要用 query string（?user=&pass=），不能用 URL userinfo。
    """
    # 只接受帶共享 secret 的請求（MediaMTX 端由 MTX_AUTHHTTPADDRESS 附上），
    # 否則此端點會成為公開的金鑰驗證 oracle
    if request.GET.get('secret') != settings.LIVE_HOOK_SECRET:
        return HttpResponse(status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    action = data.get('action')

    # 播放需要登入：驗證 room view 發給登入者的簽名 token
    if action == 'read':
        query = parse_qs(data.get('query', ''))
        token = (query.get('token') or [''])[0]
        try:
            value = _read_token_signer.unsign(token, max_age=READ_TOKEN_MAX_AGE)
        except signing.BadSignature:
            return HttpResponse(status=401)
        if value == data.get('path'):
            return HttpResponse(status=200)
        return HttpResponse(status=401)

    # 其餘非推流動作不設限
    if action != 'publish':
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
