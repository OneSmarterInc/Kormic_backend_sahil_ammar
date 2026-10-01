"""Move legacy auto-created profiles into research-only storage without losing history."""
from django.db import transaction


@transaction.atomic
def separate_profile(university_id):
    from universities.models import University
    from django_api.models import UniversityKnowledgeEntry
    from agent_queries.models import AgentConversation
    from django.db.models.deletion import Collector
    from .models import PublicUniversity, CommonAgentMessage
    profile = University.objects.select_for_update().get(pk=university_id)
    if profile.record_origin != 'researched' or profile.accounts.exists():
        return False
    research = PublicUniversity.objects.select_for_update().filter(registered_university=profile).first()
    if research is None:
        raise ValueError('Research record missing; profile retained.')
    collector = Collector(using='default')
    collector.collect([profile])
    allowed = {'universities.University', 'agent_queries.AgentConversation', 'agent_queries.AgentConversationMessage'}
    if any(model._meta.label not in allowed and rows for model, rows in collector.data.items()) or any(
            queryset.model._meta.label not in allowed and queryset.exists() for queryset in collector.fast_deletes):
        raise ValueError('Profile has additional related records; retained for review.')
    coverage = dict(research.coverage)
    coverage['catalogue_profile'] = {**{
        field:getattr(profile, field) for field in ('description','contact_email','contact_phone','eligibility_criteria')
    }, **coverage.get('catalogue_profile', {})}
    # Keep the old knowledge records as research data, not an orphaned personal KB.
    entries = UniversityKnowledgeEntry.objects.filter(university_id=str(profile.uuid))
    coverage['legacy_knowledge'] = list(entries.values('topic', 'content', 'source_type', 'source_url', 'details'))
    for conversation in AgentConversation.objects.filter(university=profile):
        for message in conversation.messages.all():
            CommonAgentMessage.objects.create(student_id=conversation.student_id, university=research,
                actor=message.actor, content=message.content, kind=message.kind, created_at=message.created_at,
                metadata={**message.metadata, 'legacy_message_id':message.pk, 'legacy_conversation_id':str(conversation.pk)})
    research.coverage, research.registered_university = coverage, None
    research.save(update_fields=['coverage', 'registered_university'])
    entries.delete()
    profile.delete()
    return True
