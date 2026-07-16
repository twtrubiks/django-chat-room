from django.contrib import admin

from .models import Room


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'is_live', 'created')
    list_filter = ('is_live',)
    search_fields = ('name', 'owner__username')
