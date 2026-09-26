from django.db import migrations


def build(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cursor.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS university_knowledge_hnsw ON django_api_universityknowledgeentry USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)")
        cursor.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS university_knowledge_topic_trgm ON django_api_universityknowledgeentry USING gin (UPPER(topic) gin_trgm_ops)")
        cursor.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS university_knowledge_content_trgm ON django_api_universityknowledgeentry USING gin (UPPER(content) gin_trgm_ops)")


def seed(apps, schema_editor):
    Knowledge = apps.get_model("django_api", "UniversityKnowledgeEntry")
    Work = apps.get_model("django_api", "KnowledgeIndexWork")
    for uid in Knowledge.objects.order_by().values_list("university_id", flat=True).distinct().iterator():
        Work.objects.get_or_create(university_id=uid, defaults={"revision": 1})
    apps.get_model("django_api", "AgentQueueGate").objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("django_api", "0004_agentqueuegate_knowledgeindexwork_agentjob")]
    operations = [migrations.RunPython(build, migrations.RunPython.noop), migrations.RunPython(seed, migrations.RunPython.noop)]
