from django.db import migrations, models
from pgvector.django import VectorExtension, VectorField


class Migration(migrations.Migration):
    dependencies = [("django_api", "0002_agentauditlog")]
    operations = [
        VectorExtension(),
        migrations.AddField(
            model_name="universityknowledgeentry", name="embedding",
            field=VectorField(dimensions=384, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="universityknowledgeentry", name="embedding_hash",
            field=models.CharField(max_length=64, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="universityknowledgeentry", name="embedding_model",
            field=models.CharField(max_length=100, blank=True, default=""),
        ),
    ]
