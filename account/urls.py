from django.urls import path
from django.contrib.auth import views as auth_views
from account import views

urlpatterns = [
    path('register/', views.register, name='register'),

    # login logout
    path('login/', auth_views.LoginView.as_view(), name='login'),

    # Django 5.0 的登出不再支援 GET 請求有關，這是一個為了提高安全性（防止 CSRF 攻擊）的重大變更，
    # 改為僅接受 POST 請求。
    path('logout/', views.logout_view, name='logout'),
]
