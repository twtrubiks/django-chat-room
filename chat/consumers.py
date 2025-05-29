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
        original_message = text_data_json['message']
        user = str(self.scope['user'])
        now_time = datetime.datetime.now().strftime(settings.DATETIME_FORMAT)

        if not original_message:
            return
        if not self.scope['user'].is_authenticated:
            return

        # 對訊息內容進行 HTML 轉義以防止 XSS
        escaped_message = escape(original_message)

        try:
            Message.objects.create(user=self.scope['user'], message=escaped_message, group_name=self.room_group_name)
        except Exception as e:
            logger.error(f"Error saving message to database: {e}")
            # 根據需要，可以決定是否仍然發送訊息或通知用戶錯誤
            return # 如果資料庫保存失敗，則不發送訊息

        # Send message to room group
        try:
            async_to_sync(self.channel_layer.group_send)(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': escaped_message, # 發送轉義後的訊息
                    'user': user,
                    'now_time': now_time
                }
            )
        except Exception as e:
            logger.error(f"Error sending message to group: {e}")

    # Receive message from room group
    def chat_message(self, event):
        # 從事件中獲取的 message 已經是轉義過的
        message = event['message']
        now_time = event['now_time']
        user = event['user']
        # Send message to WebSocket
        self.send(text_data=json.dumps({
            'message': message,
            'user': user,
            'now_time': now_time,
        }))
