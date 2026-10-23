from django.contrib.auth.models import User
from django.test import TestCase, override_settings

# 預設的 PBKDF2 刻意很慢，測試改用快速 hasher（Django 官方建議做法）
FAST_HASHER = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])


@FAST_HASHER
class RegisterViewTests(TestCase):
    def test_get_shows_form(self):
        response = self.client.get('/account/register/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'account/register.html')
        # GET 拿到的是空白表單，不能一開頁就顯示驗證錯誤
        self.assertFalse(response.context['user_form'].is_bound)
        self.assertContains(response, 'csrfmiddlewaretoken')
        # 兩個密碼欄位都必須是密碼輸入框，不能明碼顯示
        self.assertContains(response, 'type="password"', count=2)

    def test_creates_user_with_hashed_password(self):
        response = self.client.post('/account/register/', {
            'username': 'alice', 'password': 'S3cure-pass!', 'password2': 'S3cure-pass!',
        })
        self.assertTemplateUsed(response, 'account/register_done.html')
        user = User.objects.get(username='alice')
        self.assertEqual(response.context['new_user'], user)
        self.assertNotEqual(user.password, 'S3cure-pass!')
        self.assertTrue(user.check_password('S3cure-pass!'))

    def test_rejects_mismatched_passwords(self):
        response = self.client.post('/account/register/', {
            'username': 'alice', 'password': 'S3cure-pass!', 'password2': 'other',
        })
        self.assertTemplateUsed(response, 'account/register.html')
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertEqual(response.context['user_form'].errors['password2'], ["Passwords don't match."])
        # 驗證失敗重新顯示表單時，不能把使用者輸入的密碼寫回 HTML
        self.assertNotContains(response, 'S3cure-pass!')
        self.assertNotContains(response, 'value="other"')
        self.assertFalse(User.objects.exists())

    def test_rejects_missing_password(self):
        response = self.client.post('/account/register/', {
            'username': 'alice', 'password': '', 'password2': 'S3cure-pass!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('password', response.context['user_form'].errors)
        self.assertFalse(User.objects.exists())


@FAST_HASHER
class LoginLogoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='S3cure-pass!')

    def test_login_redirects_to_chat(self):
        response = self.client.post('/account/login/', {
            'username': 'alice', 'password': 'S3cure-pass!',
        })
        self.assertRedirects(response, '/chat/')

    def test_logout(self):
        self.client.force_login(self.user)
        response = self.client.post('/account/logout/')
        self.assertRedirects(response, '/account/login/')
        self.assertNotIn('_auth_user_id', self.client.session)
