# docker-django-chat-room

docker-django-chat-room use django-channels3💬

* [Youtube Tutorial - chat-room use django-channels2](https://youtu.be/CoVdpZLCIT4)

## 前言

這篇文章是 [django-channels2-tutorial](https://github.com/twtrubiks/django-channels2-tutorial) 的改良版，主要加上美化以及資料庫。

## 執行畫面

直接瀏覽 [http://localhost:8000/chat/](http://localhost:8000/chat/)

![alt tag](https://i.imgur.com/5b2kTBR.png)

未登入無法聊天（ 也無法看到聊天內容 ）

![alt tag](https://i.imgur.com/UrdlV1e.png)

登入註冊頁面

![alt tag](https://i.imgur.com/Gpt0gwT.png)

聊天畫面

![alt tag](https://i.imgur.com/gT8hJ89.png)

## 如何執行

確認電腦有安裝 docker 後，直接執行以下指令即可，

```cmd
docker-compose up
```

![alt tag](https://i.imgur.com/R5mXwQU.png)

如何移除 ( 包含移除 volume )，

```cmd
docker-compose down -v
```

詳細的詳細說明，在上一篇 [django-channels2-tutorial](https://github.com/twtrubiks/django-channels2-tutorial) 都說明過了。

## 其他說明

### 說明一

登入註冊是參考之前寫的教學完成，可參考 [Django Social Login Tutorial](https://github.com/twtrubiks/django_social_login_tutorial)，我有簡化一些不必要的部份，

但大多都是直接拿過來使用的。

### 說明二

為什麼需要使用到 [reconnecting-websocket](https://github.com/joewalnes/reconnecting-websocket)，原因是假設當 websocket 斷線時，他並不會自動重新連結，

我們可以透過這個 js ，幫我們完成當 websocket 斷線時，自動重新連線的工作。

### 說明三

js 中的 `{{ room_name }}` 用法，像是在 django_chat_room/chat/templates/chat/[room.html](https://github.com/twtrubiks/django-chat-room/blob/master/chat/templates/chat/room.html) 中的

```javascript
....
$('#all_messages').scrollTop($('#all_messages')[0].scrollHeight);
var room_mame = '{{ room_name }}';
var ws_scheme = window.location.protocol == "https:" ? "wss" : "ws";
var websocket_str= ws_scheme+'://' + window.location.host + '/ws/chat/' + room_mame + '/';
var chatSocket = new ReconnectingWebSocket(websocket_str);
...
```

為什麼可以使用 `{{ room_name }}` 這種寫法，原因是 [jinja2](http://jinja.pocoo.org/) 的關係，不過這樣就不能單獨把這段 js 獨立

出來成一隻 .js 檔案，因為這樣的話 jinja2 就會失效，但如果真的硬要抽出來也是可以，我們可以把它 render

到某個 hidden 的欄位，然後再用 js 去抓他的值。

## 後記

依照 [django-channels2-tutorial](https://github.com/twtrubiks/django-channels2-tutorial) 這篇的基本觀念，延伸出一個比較完整的聊天室，

整體來說，蠻好玩的 :satisfied: 這邊一樣沒有佈署，本來想佈署到 Heroku，但因為需要用

到 redis ( 要信用卡 )，所以就沒佈署了，相關的部屬流程，可參考官網的 [Deploying](https://channels.readthedocs.io/en/latest/deploying.html)。

## 執行環境

* Python 3.8

## Reference

* [Django](https://www.djangoproject.com/)
* [Channels](https://github.com/django/channels)
* [reconnecting-websocket](https://github.com/joewalnes/reconnecting-websocket)
* [聊天室版型](https://bootsnipp.com/snippets/featured/chat-widget)

## Donation

文章都是我自己研究內化後原創，如果有幫助到您，也想鼓勵我的話，歡迎請我喝一杯咖啡:laughing:

![alt tag](https://i.imgur.com/LRct9xa.png)

[贊助者付款](https://payment.opay.tw/Broadcaster/Donate/9E47FDEF85ABE383A0F5FC6A218606F8)

## License

MIT licens
