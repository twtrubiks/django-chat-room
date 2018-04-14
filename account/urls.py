from django.contrib.auth.views import login
from django.contrib.auth.views import logout
from django.contrib.auth.views import logout_then_login
from django.urls import path

from account import views

urlpatterns = [
    # path('', views.dashboard, name='dashboard'),
    path('register/', views.register, name='register'),

    # login logout
    path('login/', login, name='login'),
    path('logout/', logout, name='logout'),
    path('logout-then-login/', logout_then_login, name='logout_then_login'),

]
