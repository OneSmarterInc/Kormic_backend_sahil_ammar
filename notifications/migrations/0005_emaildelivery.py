import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('notifications', '0004_pushdelivery')]
    operations = [migrations.CreateModel(
        name='EmailDelivery',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('available_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ('attempts', models.PositiveIntegerField(default=0)),
            ('sent_at', models.DateTimeField(blank=True, null=True)),
            ('last_error', models.CharField(blank=True, max_length=100)),
            ('notification', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='notifications.notificationlog')),
        ],
    )]
