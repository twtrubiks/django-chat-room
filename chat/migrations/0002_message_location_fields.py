from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='message',
            name='message',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='message',
            name='message_type',
            field=models.CharField(
                choices=[('text', 'text'), ('location', 'location')],
                default='text',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='message',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='message',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
    ]
