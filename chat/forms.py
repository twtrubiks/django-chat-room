from django import forms

from .models import Room


class RoomCreateForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ['name']
        labels = {'name': '房間名稱'}
        help_texts = {'name': '只能包含字母、數字與底線，會成為直播網址的一部分'}

    def clean_name(self):
        name = self.cleaned_data['name']
        # 'create' 是 /chat/create/ 的保留路徑，撞名的房間會永遠進不去
        if name == 'create':
            raise forms.ValidationError('此名稱為系統保留，請換一個')
        return name
