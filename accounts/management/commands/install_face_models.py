from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.face_models import install_models, validate_models


class Command(BaseCommand):
    help = 'Install the supplied liveness archive and validate hashes and CPU ONNX loading.'

    def add_arguments(self, parser):
        parser.add_argument('archive', nargs='?')
        parser.add_argument('--directory', default=str(Path(settings.BASE_DIR) / 'face_models'))

    def handle(self, *args, **options):
        try:
            if options['archive']:
                install_models(options['archive'], options['directory'])
            for row in validate_models(options['directory']):
                self.stdout.write(f"PASS {row['model']}: SHA256 verified; CPU ONNX loaded")
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write('Model readiness only. This does not certify live capture or identity matching.')
