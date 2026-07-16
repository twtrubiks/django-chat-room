import json
import uuid

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Message, Room


# Create your views here.

def index(request):
    uid4 = uuid.uuid4().hex
    return render(request, 'chat/index.html', {'uid4': uid4})


def room(request, room_name):
    chat_messages = Message.objects.filter(group_name=room_name).order_by("created")[:100]
    # 有註冊 Room 的房名才有直播功能，否則維持純聊天室
    live_room = Room.objects.filter(name=room_name).first()
    return render(request, 'chat/room.html', {
        'chat_messages': chat_messages,
        'room_name': room_name,
        'live_room': live_room,
    })


@csrf_exempt
@require_POST
def stream_auth(request):
    """
    MediaMTX authHTTPAddress 委派認證端點，回應 20x 放行、其他拒絕。
    推流帳密要用 query string（?user=&pass=），不能用 URL userinfo。
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    # 只限制推流，播放不設限
    if data.get('action') != 'publish':
        return HttpResponse(status=200)

    room = Room.objects.filter(name=data.get('path', '')).first()
    if room and data.get('password') == room.stream_key:
        return HttpResponse(status=200)
    return HttpResponse(status=401)
