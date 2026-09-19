import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from drf_spectacular.generators import SchemaGenerator
from django.conf import settings


class Command(BaseCommand):
    help = 'Export serializer-derived client snapshots or fail on committed drift.'

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true')
        parser.add_argument('--output', default=str(settings.BASE_DIR / 'contracts'))

    def handle(self, *args, **options):
        schema = SchemaGenerator(urlconf='django_api.contract_urls').get_schema(request=None, public=True)
        out = Path(options['output'])
        for filename, component in [('student-profile.openapi.json', 'ProfileCreateUpdate'), ('portal-user.openapi.json', 'PortalUser')]:
            document = {'openapi': schema['openapi'], 'info': {'title': 'Kormic client contract', 'version': '1.0.0'}, 'components': {'schemas': {component: schema['components']['schemas'][component]}}}
            content = json.dumps(document, indent=2, ensure_ascii=False) + '\n'
            path = out / filename
            if options['check']:
                if not path.exists() or json.loads(path.read_text()) != document:
                    raise CommandError(f'{filename} differs from the serializer; regenerate and update client snapshots')
            else:
                out.mkdir(parents=True, exist_ok=True); path.write_text(content)
