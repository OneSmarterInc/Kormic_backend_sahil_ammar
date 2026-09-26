from django.db import migrations


def build(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('CREATE EXTENSION IF NOT EXISTS vector')
            cursor.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS public_university_fact_hnsw ON university_research_universityfact USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)')


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('university_research', '0001_initial'), ('django_api', '0003_university_knowledge_vectors')]
    operations = [migrations.RunPython(build, migrations.RunPython.noop)]
