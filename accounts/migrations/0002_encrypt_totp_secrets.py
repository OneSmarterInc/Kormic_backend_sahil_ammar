from django.db import migrations, models


def encrypt_existing_secrets(apps, schema_editor):
    from accounts.crypto import encrypt_totp_secret, totp_keyring

    # Validate configuration even on a fresh/empty database. No plaintext
    # fallback and no key derived from DATABASE/SECRET_KEY settings.
    totp_keyring()
    Device = apps.get_model('accounts', 'TOTPDevice')
    alias = schema_editor.connection.alias
    for device in Device.objects.using(alias).all().iterator(chunk_size=500):
        device.secret_encrypted = encrypt_totp_secret(device.secret_encrypted)
        device.save(using=alias, update_fields=['secret_encrypted'])


class Migration(migrations.Migration):
    atomic = True
    dependencies = [('accounts', '0001_initial')]
    operations = [
        migrations.RenameField('totpdevice', 'secret', 'secret_encrypted'),
        migrations.AlterField('totpdevice', 'secret_encrypted', models.TextField(editable=False)),
        # Intentionally irreversible: rolling back must never restore plaintext.
        migrations.RunPython(encrypt_existing_secrets),
    ]
