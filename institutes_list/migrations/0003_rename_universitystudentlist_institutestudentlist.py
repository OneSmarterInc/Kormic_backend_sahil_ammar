from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("institutes_list", "0002_roster_country_region"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="UniversityStudentList",
            new_name="InstituteStudentList",
        ),
    ]
