import uuid

from django.shortcuts import render

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
