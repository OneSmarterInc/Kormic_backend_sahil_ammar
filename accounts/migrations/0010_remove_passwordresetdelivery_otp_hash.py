from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_password_reset_delivery"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="passwordresetdelivery",
            name="otp_hash",
        ),
    ]
