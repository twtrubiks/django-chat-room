import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0003_room'),
    ]

    operations = [
        migrations.AlterField(
            model_name='room',
            name='name',
            field=models.SlugField(max_length=64, unique=True, validators=[django.core.validators.RegexValidator('^\\w+$', '房間名稱只能包含字母、數字與底線')]),
        ),
    ]
