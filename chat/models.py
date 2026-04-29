from django.contrib.auth.models import User
from django.db import models


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
