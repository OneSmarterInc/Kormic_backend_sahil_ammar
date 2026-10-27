from django.db.models import Count, Max, Q


def section_filter(section):
    if section == 'queries':
        return Q(event_type__in=['university_query', 'pending_query_resolved']) | Q(data__route='queries')
    source = {'resumes': 'resume', 'githubProfile': 'github', 'linkedin': 'linkedin'}.get(section)
    if source:
        return Q(event_type='profile_processed', data__source=source)
    return Q(pk__in=[])


SECTIONS = ('queries', 'resumes', 'githubProfile', 'linkedin')


def unread_sections(account):
    from .models import NotificationLog
    rows = NotificationLog.objects.filter(account=account, read_at__isnull=True, dismissed_at__isnull=True)
    totals = rows.aggregate(**{
        key: expression for section in SECTIONS for key, expression in (
            (section + '_count', Count('pk', filter=section_filter(section))),
            (section + '_latest', Max('pk', filter=section_filter(section))),
        )
    })
    return {section: {'count': totals[section + '_count'], 'latest_id': totals[section + '_latest']} for section in SECTIONS}
