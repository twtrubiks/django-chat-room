from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='chat'),
    # 需在 <str:room_name> 之前，否則會被當成房名
    path('create/', views.create_room, name='create_room'),
    path('<str:room_name>/regenerate_key/', views.regenerate_stream_key, name='regenerate_stream_key'),
    path('<str:room_name>/', views.room, name='room'),
]
