from django.db import migrations


def assign_departments(apps, schema_editor):
    from universities.knowledge_groups import classify_group_slug
    Query = apps.get_model('agent_queries', 'AgentQuery')
    Group = apps.get_model('universities', 'KnowledgeGroup')
    groups = {(g.university_id, g.slug): g.pk for g in Group.objects.all()}
    for query in Query.objects.filter(group__isnull=True).select_related('conversation').iterator():
        group_id = groups.get((query.conversation.university_id, classify_group_slug(query.question)))
        if group_id:
            Query.objects.filter(pk=query.pk, group__isnull=True).update(group_id=group_id)


class Migration(migrations.Migration):
    dependencies = [('agent_queries', '0004_agentquery_group')]
    operations = [migrations.RunPython(assign_departments, migrations.RunPython.noop)]
