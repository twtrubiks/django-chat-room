import datetime
import json
import logging # 新增 logging 模組

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from django.conf import settings
from django.utils.html import escape # 新增 escape 函數

from .models import Message

# 獲取 logger 實例
logger = logging.getLogger(__name__)


class ChatConsumer(WebsocketConsumer):
    def connect(self):
        self.room_group_name = self.scope['url_route']['kwargs']['room_name']

        # Join room group
        async_to_sync(self.channel_layer.group_add)(
            self.room_group_name,
            self.channel_name
        )

        if self.scope["user"].is_anonymous:
            self.close()
        else:
            self.accept()

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(
            self.room_group_name,
            self.channel_name
        )

    # Receive message from WebSocket
    def receive(self, text_data):
        text_data_json = json.loads(text_data)
        msg_type = text_data_json.get('type', 'text')
        user = str(self.scope['user'])
        now_time = datetime.datetime.now().strftime(settings.DATETIME_FORMAT)

        if not self.scope['user'].is_authenticated:
            return

        if msg_type == 'location':
            self._handle_location(text_data_json, user, now_time)
        else:
            self._handle_text(text_data_json, user, now_time)

    def _handle_text(self, payload, user, now_time):
        original_message = payload.get('message', '')
        if not original_message:
            return

        # 對訊息內容進行 HTML 轉義以防止 XSS
        escaped_message = escape(original_message)

        try:
            Message.objects.create(
                user=self.scope['user'],
                message=escaped_message,
                group_name=self.room_group_name,
                message_type=Message.TEXT,
            )
        except Exception as e:
            logger.error(f"Error saving message to database: {e}")
            return

        try:
            async_to_sync(self.channel_layer.group_send)(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message_type': Message.TEXT,
                    'message': escaped_message,
                    'user': user,
                    'now_time': now_time,
                }
            )
        except Exception as e:
            logger.error(f"Error sending message to group: {e}")

    def _handle_location(self, payload, user, now_time):
        try:
            latitude = float(payload['latitude'])
            longitude = float(payload['longitude'])
        except (KeyError, TypeError, ValueError):
            logger.warning("Invalid location payload: %s", payload)
            return

        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            logger.warning("Location out of range: lat=%s lng=%s", latitude, longitude)
            return

        try:
            Message.objects.create(
                user=self.scope['user'],
                message='',
                group_name=self.room_group_name,
                message_type=Message.LOCATION,
                latitude=latitude,
                longitude=longitude,
            )
        except Exception as e:
            logger.error(f"Error saving location to database: {e}")
            return

        try:
            async_to_sync(self.channel_layer.group_send)(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message_type': Message.LOCATION,
                    'latitude': latitude,
                    'longitude': longitude,
                    'user': user,
                    'now_time': now_time,
                }
            )
        except Exception as e:
            logger.error(f"Error sending location to group: {e}")

    # Receive stream status from live hook (via group_send)
    def stream_status(self, event):
        self.send(text_data=json.dumps({
            'message_type': 'stream_status',
            'is_live': event['is_live'],
        }))

    # Receive message from room group
    def chat_message(self, event):
        message_type = event.get('message_type', 'text')
        payload = {
            'message_type': message_type,
            'user': event['user'],
            'now_time': event['now_time'],
        }
        if message_type == 'location':
            payload['latitude'] = event['latitude']
            payload['longitude'] = event['longitude']
        else:
            payload['message'] = event['message']
        # Send message to WebSocket
        self.send(text_data=json.dumps(payload))
