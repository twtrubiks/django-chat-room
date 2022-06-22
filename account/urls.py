from django.urls import path
from django.contrib.auth import views as auth_views
from account import views

urlpatterns = [
    path('register/', views.register, name='register'),

    # login logout
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/',auth_views.LogoutView.as_view(), name='logout'),
]
