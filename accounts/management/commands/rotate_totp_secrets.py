from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from accounts.crypto import TOTPEncryptionError, totp_keyring
from accounts.models import TOTPDevice


class Command(BaseCommand):
    help = 'Re-encrypt TOTP seeds with the first TOTP_SECRET_KEYS key, or verify that key can decrypt every row.'

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true', help='Read-only: verify every seed with the primary key alone.')

    def handle(self, *args, **options):
        count = 0
        try:
            ring = totp_keyring()
            primary = Fernet(settings.TOTP_SECRET_KEYS[0].encode('ascii'))
            # Lock rows for rotation so concurrent enrollment cannot be overwritten.
            # All rows roll back on a missing key or corrupted ciphertext.
            with transaction.atomic():
                devices = TOTPDevice.objects.order_by('pk')
                if not options['check']:
                    devices = devices.select_for_update()
                for device in devices.iterator(chunk_size=500):
                    if options['check']:
                        primary.decrypt(device.secret_encrypted.encode('ascii'))
                    else:
                        device.secret_encrypted = ring.rotate(device.secret_encrypted.encode('ascii')).decode('ascii')
                        device.save(update_fields=['secret_encrypted'])
                    count += 1
        except (TOTPEncryptionError, InvalidToken, UnicodeError) as exc:
            raise CommandError('TOTP key verification failed. No rotation changes were committed; restore the required key(s) or investigate corrupted data.') from exc
        action = 'Verified' if options['check'] else 'Rotated'
        self.stdout.write(self.style.SUCCESS(f'{action} {count} TOTP device(s).'))
