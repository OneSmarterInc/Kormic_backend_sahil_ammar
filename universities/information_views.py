from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from django_api.models import UniversityKnowledgeEntry
from universities.models import University
from universities.views import UNIVERSITY_ADMIN_PERMISSIONS, _get_own_university, _error, _serialize_knowledge_entry, KnowledgeFactDetailAPIView
from universities.information import research_models, research_record, OVERVIEW_FIELDS


class InformationListView(APIView):
    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        university = _get_own_university(request)
        if university is None:
            return _error("University not found.", 404)
        entries = list(UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid)).select_related("group").defer("embedding"))
        keys = {entry.details.get("_information_research_key") for entry in entries}
        records = [{**_serialize_knowledge_entry(entry),
                    **({'record_kind': 'overview'} if entry.details.get('_information_overview') else {})}
                   for entry in entries]
        for kind, model in research_models().items():
            rows = model.objects.filter(university__registered_university=university)
            if kind != "page":
                rows = rows.select_related("page")
            for row in rows:
                item = research_record(kind, row)
                if item["research_key"] not in keys:
                    records.append(item)
        if 'page' in request.query_params:
            from django.core.paginator import Paginator
            try:
                page_number = int(request.query_params['page'])
                if page_number < 1:
                    raise ValueError
            except (ValueError, TypeError):
                return _error('page must be a positive integer.')
            other_count = sum(item.get('category', 'other') == 'other' for item in records)
            counts = {'other': other_count, 'categories': len(records) - other_count}
            group = request.query_params.get('category_group')
            if group in {'other', 'categories'}:
                records = [item for item in records if (item.get('category', 'other') == 'other') == (group == 'other')]
            search = request.query_params.get('search', '').strip().casefold()
            if search:
                import json
                records = [item for item in records if search in
                    (str(item.get('topic', '')) + ' ' + str(item.get('content', '')) + ' ' + json.dumps(item.get('details', {}), ensure_ascii=False)).casefold()]
            records.sort(key=lambda item: str(item['id']))
            paginator = Paginator(records, 50)
            page = paginator.get_page(page_number)
            return Response({'knowledge': list(page), 'count': paginator.count, 'page': page.number,
                             'pages': paginator.num_pages, 'category_counts': counts})
        return Response({"knowledge": records})

    @transaction.atomic
    def post(self, request):
        from universities.views import KnowledgeFactListCreateAPIView
        created = KnowledgeFactListCreateAPIView().post(request)
        if created.status_code >= 400:
            return created
        updated = KnowledgeFactDetailAPIView().patch(request, created.data['id'], checked_revision=True)
        if updated.status_code >= 400:
            transaction.set_rollback(True)
            return updated
        return Response(updated.data, status=201)


class InformationEntitiesView(InformationListView):
    @transaction.atomic
    def post(self, request):
        from universities.structured_information import KINDS, specific_name, identity
        details = request.data.get('details')
        if not isinstance(details, dict) or details.get('information_type') not in KINDS - {'overview'}:
            return _error('Choose a valid information section.')
        if not specific_name(details.get('name')):
            return _error('Enter a specific name, such as the course or scholarship name.')
        university = _get_own_university(request)
        if university is None:
            return _error('University not found.', 404)
        University.objects.select_for_update().get(pk=university.pk)
        key = identity(details['information_type'], details['name'])
        if UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), details___information_entity_key=key).exists():
            return _error('A record with this name already exists. Edit the existing record.', 409)
        response = super().post(request)
        if response.status_code == 201:
            row = UniversityKnowledgeEntry.objects.get(pk=response.data['id'])
            row.details['_information_entity_key'] = key
            row.save()
            return Response(_serialize_knowledge_entry(row), status=201)
        return response

    def get(self, request):
        from universities.structured_information import specific_name
        university = _get_own_university(request)
        if university is None:
            return _error('University not found.', 404)
        records = []
        rows = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid),
            details__has_key='information_type').defer('embedding').select_related('group')
        for row in rows:
            if row.details.get('information_type') == 'overview' or not specific_name(row.details.get('name')):
                continue
            if not row.details.get('_information_entity_key') and row.source_type not in {'human_verified', 'manual'}:
                continue
            record = _serialize_knowledge_entry(row)
            record['source_urls'] = sorted(set(row.details.get('_information_source_values', {})) | {value['url'] for value in row.details.get('_information_field_sources', {}).values() if value.get('url')})
            record['autofilled'] = row.source_type == 'scraped'
            records.append(record)
        from universities.information_costs import place_costs
        return Response({'knowledge': place_costs(records)})


def overview_record(university):
    import hashlib
    import json
    values = {key: getattr(university, key) for key in OVERVIEW_FIELDS}
    confirmed = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), details___information_overview=True).exists()
    if not confirmed:
        for entry in UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid),
                details__information_type='overview', source_type='scraped').defer('embedding'):
            for key in OVERVIEW_FIELDS:
                if not values[key] and entry.details.get(key):
                    values[key] = entry.details[key]
    return {'values': values, 'revision': hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()}


class InformationOverviewView(APIView):
    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    def get(self, request):
        university = _get_own_university(request)
        return Response(overview_record(university)) if university else _error('University not found.', 404)

    @transaction.atomic
    def patch(self, request):
        from django.core.validators import validate_email, URLValidator
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        from universities import services
        university = _get_own_university(request)
        if university is None:
            return _error('University not found.', 404)
        university = University.objects.select_for_update().get(pk=university.pk)
        if request.data.get('expected_revision') != overview_record(university)['revision']:
            return _error('University details changed. Reload before saving.', 409)
        values = request.data.get('values')
        if not isinstance(values, dict) or set(values) - set(OVERVIEW_FIELDS):
            return _error('Choose valid university detail fields.')
        for key, value in values.items():
            if not isinstance(value, str):
                return _error(f'{key} must be text.')
            limit = University._meta.get_field(key).max_length or 20000
            if len(value) > limit:
                return _error(f'{key} must be at most {limit} characters.')
            setattr(university, key, value.strip())
        if not university.name:
            return _error('University name is required.')
        try:
            if university.contact_email:
                validate_email(university.contact_email)
            if university.website_url:
                URLValidator(schemes=['https', 'http'])(university.website_url)
        except ValidationError:
            return _error('Enter a valid email address and website URL.')
        university.save(update_fields=[*values, 'updated_at'])
        services.sync_profile_facts_to_kb(university)
        fields = {key: getattr(university, key) for key in OVERVIEW_FIELDS}
        entry = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid),
            details___information_overview=True).first()
        if entry is None:
            entry = UniversityKnowledgeEntry(university_id=str(university.uuid))
        entry.topic = 'University overview and contact details'
        entry.content = '\n'.join(f"{key.replace('_', ' ').title()}: {value}" for key, value in fields.items() if value)
        entry.details = {**fields, '_information_overview': True, '_information_category': 'overview',
                         '_information_edited_at': timezone.now().isoformat()}
        entry.source_type, entry.confidence = 'human_verified', 1.0
        entry.save()
        return Response(overview_record(university))


class ResearchInformationDetailView(APIView):
    permission_classes = UNIVERSITY_ADMIN_PERMISSIONS

    @transaction.atomic
    def patch(self, request, kind, record_id):
        university = _get_own_university(request)
        model = research_models().get(kind)
        if university is None or model is None:
            return _error("Information not found.", 404)
        # Serialize creation of an override without adding a second fact when
        # two officers save the same research record simultaneously.
        University.objects.select_for_update().get(pk=university.pk)
        row = model.objects.filter(pk=record_id, university__registered_university=university).first()
        if row is None:
            return _error("Information changed or was removed. Reload the page.", 404)
        item = research_record(kind, row)
        existing = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), details___information_research_key=item["research_key"]).first()
        if existing or request.data.get("expected_revision") != item["revision"]:
            return _error("This information changed since you opened it. Reload before saving.", 409)
        entry = UniversityKnowledgeEntry.objects.create(
            university_id=str(university.uuid), topic=item["topic"], content=item["content"],
            source_type="human_verified", source_url=item["source_url"], details={
                **item["details"], "_information_research_key": item["research_key"],
                "_information_research_kind": kind, "_information_category": item["category"],
            },
        )
        response = KnowledgeFactDetailAPIView().patch(request, entry.pk, checked_revision=True)
        if response.status_code >= 400:
            transaction.set_rollback(True)
        return response
