from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .forms import UserRegistrationForm
from django.contrib.auth import logout
from django.shortcuts import redirect


@login_required
def dashboard(request):
    return render(request, 'chat/index.html', {})


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

def logout_view(request):
    logout(request)
    return redirect('login')