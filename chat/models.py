import secrets

from django.contrib.auth.models import User
from django.db import models


def generate_stream_key():
    return secrets.token_hex(16)


class Room(models.Model):
    """
    直播間。name 對應 MediaMTX 的 stream path（一對一），
    未註冊 Room 的房名維持原本純聊天室行為（無直播功能）。
    """

    name = models.SlugField(max_length=64, unique=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='rooms')
    stream_key = models.CharField(max_length=64, default=generate_stream_key)
    is_live = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Message(models.Model):
    """
    Model to represent user submitted changed to a resource guide
    """

    TEXT = 'text'
    LOCATION = 'location'
    MESSAGE_TYPE_CHOICES = [
        (TEXT, 'text'),
        (LOCATION, 'location'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    group_name = models.TextField()
    message = models.TextField(blank=True)
    message_type = models.CharField(
        max_length=10,
        choices=MESSAGE_TYPE_CHOICES,
        default=TEXT,
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        """
        String to represent the message
        """

        if self.message_type == self.LOCATION:
            return f'[location {self.latitude}, {self.longitude}]'
        return self.message
