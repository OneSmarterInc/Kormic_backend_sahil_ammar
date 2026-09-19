from pathlib import Path
from django.conf import settings
from django.db import migrations


def normalize_paths(apps, schema_editor):
    model = apps.get_model('institutes_list', 'UniversityStudentList')
    root = Path(settings.MEDIA_ROOT).resolve()
    for source in model.objects.exclude(source_file_path='').iterator():
        path = Path(source.source_file_path)
        if path.is_absolute():
            try:
                relative = path.resolve().relative_to(root)
            except ValueError:
                # An unknown historical root requires operator review, not guessing.
                continue
            model.objects.filter(pk=source.pk).update(source_file_path=relative.as_posix())


class Migration(migrations.Migration):
    dependencies = [('institutes_list', '0001_initial')]
    operations = [migrations.RunPython(normalize_paths, migrations.RunPython.noop)]
