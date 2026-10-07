from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("universities", "0006_remove_university_university_country_us_and_more"),
        ("url_discovery", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="scope",
            field=models.CharField(choices=[("all", "All saved URLs"), ("selected", "Approved URL selection")], default="all", max_length=16),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="cluster_approval",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="scrape_jobs", to="url_discovery.discoveryclusterapproval",
            ),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="selected_urls",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="progress_completed",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="progress_total",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="current_url",
            field=models.TextField(blank=True, default=""),
        ),
    ]
