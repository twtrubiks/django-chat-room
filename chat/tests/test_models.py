import io

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from chat.forms import RoomCreateForm
from chat.models import Message, Room, generate_stream_key


class MigrationTests(TestCase):
    def test_models_match_migrations(self):
        # entrypoint.sh 啟動時會自動 makemigrations，漏 commit 的 migration 會在各環境各自產生
        call_command('makemigrations', '--check', '--dry-run', stdout=io.StringIO())


class RoomModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner')

    def test_generate_stream_key(self):
        key = generate_stream_key()
        self.assertRegex(key, r'^[0-9a-f]{32}$')
        self.assertNotEqual(key, generate_stream_key())

    def test_defaults(self):
        room = Room.objects.create(name='myroom', owner=self.owner)
        other = Room.objects.create(name='other', owner=self.owner)
        self.assertFalse(room.is_live)
        self.assertRegex(room.stream_key, r'^[0-9a-f]{32}$')
        # default 必須是 callable，每間房各自一把金鑰
        self.assertNotEqual(room.stream_key, other.stream_key)
        self.assertEqual(str(room), 'myroom')
        self.assertEqual(list(self.owner.rooms.order_by('name')), [room, other])

    def test_name_validation(self):
        Room(name='my_room_1', owner=self.owner).full_clean()
        Room(name='a' * 64, owner=self.owner).full_clean()
        for name in ['bad-name', 'a' * 65, '']:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                Room(name=name, owner=self.owner).full_clean()

    def test_name_rejects_dash_with_message(self):
        # SlugField 允許 '-'，但 WebSocket routing 只吃 \w+
        with self.assertRaises(ValidationError) as ctx:
            Room(name='bad-name', owner=self.owner).full_clean()
        self.assertIn('房間名稱只能包含字母、數字與底線', ctx.exception.message_dict['name'])


class MessageModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice')

    def test_text_message(self):
        message = Message.objects.create(user=self.user, group_name='room', message='hello')
        self.assertEqual(message.message_type, Message.TEXT)
        self.assertEqual(str(message), 'hello')

    def test_location_message(self):
        created = Message.objects.create(user=self.user, group_name='room', message_type=Message.LOCATION,
                                         latitude=25.03, longitude=121.56)
        # 從 DB 讀回：message_type 是新的字串物件，不是 Message.LOCATION 常數本身
        message = Message.objects.get(pk=created.pk)
        self.assertEqual(str(message), '[location 25.03, 121.56]')


class RoomCreateFormTests(TestCase):
    def test_valid_name(self):
        # 字典序在 'create' 前後的名稱都要能用
        for name in ['abc', 'myroom', 'created']:
            with self.subTest(name=name):
                self.assertTrue(RoomCreateForm({'name': name}).is_valid())

    def test_rejects_reserved_name(self):
        # /chat/create/ 是保留路徑，撞名的房間永遠進不去
        form = RoomCreateForm({'name': 'create'})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['name'], ['此名稱為系統保留，請換一個'])

    def test_rejects_duplicate_name(self):
        Room.objects.create(name='myroom', owner=User.objects.create_user(username='owner'))
        self.assertFalse(RoomCreateForm({'name': 'myroom'}).is_valid())

    def test_only_name_is_editable(self):
        self.assertEqual(list(RoomCreateForm().fields), ['name'])


class RoomAdminTests(TestCase):
    def test_changelist_with_search_and_filter(self):
        superuser = User.objects.create_superuser(username='admin', email='a@example.com')
        Room.objects.create(name='myroom', owner=superuser, is_live=True)
        self.assertIn(Room, admin.site._registry)
        self.client.force_login(superuser)
        response = self.client.get('/admin/chat/room/', {'q': 'admin', 'is_live__exact': '1'})
        self.assertContains(response, 'myroom')


class UrlContractTests(TestCase):
    """這些路徑寫死在 mediamtx.yml、前端 JS 與 README，不能隨意更動"""

    def test_paths(self):
        self.assertEqual(reverse('chat'), '/chat/')
        self.assertEqual(reverse('create_room'), '/chat/create/')
        self.assertEqual(reverse('room', args=['myroom']), '/chat/myroom/')
        self.assertEqual(reverse('regenerate_stream_key', args=['myroom']), '/chat/myroom/regenerate_key/')
        self.assertEqual(reverse('delete_room', args=['myroom']), '/chat/myroom/delete/')
        self.assertEqual(reverse('stream_auth'), '/live/auth/')
        self.assertEqual(reverse('stream_hook'), '/live/hook/')
        self.assertEqual(reverse('register'), '/account/register/')
        self.assertEqual(reverse('login'), '/account/login/')
        self.assertEqual(reverse('logout'), '/account/logout/')
