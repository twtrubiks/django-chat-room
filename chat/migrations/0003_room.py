import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import chat.models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('chat', '0002_message_location_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='Room',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.SlugField(max_length=64, unique=True)),
                ('stream_key', models.CharField(default=chat.models.generate_stream_key, max_length=64)),
                ('is_live', models.BooleanField(default=False)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rooms', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
