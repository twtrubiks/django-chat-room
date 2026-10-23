from django.shortcuts import render
from django.views.decorators.http import require_POST

from .forms import UserRegistrationForm
from django.contrib.auth import logout
from django.shortcuts import redirect


def register(request):
    if request.method == 'POST':
        user_form = UserRegistrationForm(request.POST)
        if user_form.is_valid():
            # Create a new user object but avoid saving it yet
            new_user = user_form.save(commit=False)
            # Set the chosen password
            new_user.set_password(
                user_form.cleaned_data['password'])
            # Save the User object
            new_user.save()
            return render(request,
                          'account/register_done.html',
                          {'new_user': new_user})
    else:
        user_form = UserRegistrationForm()
    return render(request,
                  'account/register.html',
                  {'user_form': user_form})


# 只接受 POST：GET 登出可被其他網站用 <img src="/account/logout/"> 觸發（CSRF 登出）
@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')