from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('django_api', '0012_agent_fairness_recovery')]

    operations = [migrations.AlterField(
        model_name='studentdocumentevidence', name='status',
        field=models.CharField(default='read', max_length=32),
    )]
