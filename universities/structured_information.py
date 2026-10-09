"""Source-scoped entities for the fixed university information forms.

This adapter reads document structure, never turns generic knowledge topics into
courses/awards, and never shares one award's criteria with another award.
"""
import hashlib
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

KINDS = {'academics', 'scholarships', 'housing', 'fees', 'admissions', 'international', 'campus', 'careers', 'other', 'overview'}
GENERIC = {'courses', 'course', 'programs', 'program', 'research', 'scholarships', 'scholarship',
    'scholarship eligibility', 'eligibility', 'eligibility criteria', 'scholarship details',
    'scholarship policies', 'scholarship appeal', 'scholarship search', 'available scholarships',
    'about scholarships', 'how to apply for scholarships', 'housing', 'residence life and housing',
    'international undergraduate student scholarships', 'international graduate student scholarships',
    'first year scholarships', 'transfer scholarships', 'continuing student scholarships',
    'student activities and organizations', 'tuition and fees', 'fees', 'admissions'}


def clean(value):
    return ' '.join(str(value or '').split())


def identity(kind, name):
    normalized = re.sub(r'[^\w]', '', clean(name).casefold())
    return hashlib.sha256(f'{kind}:{normalized}'.encode()).hexdigest()


def specific_name(name):
    normalized = clean(name).casefold().replace('-', ' ').strip(' :.')
    if not normalized or normalized in GENERIC:
        return False
    if 'scholarship' in normalized and re.search(r'notification|opportunities|requests?|academic component|details|programs$', normalized):
        return False
    return True


def text(node):
    return clean(node.get_text(' ', strip=True))


def blocks(nodes):
    result = []
    for node in nodes:
        if getattr(node, 'name', '') in {'p', 'li', 'tr'}:
            value = text(node)
            if value and value not in result:
                result.append(value)
    return result


def scope_after(heading, stop_at_award=False):
    rank = int(heading.name[1])
    result = []
    for node in heading.next_elements:
        if stop_at_award and getattr(node, 'name', '') in {'h1', 'h2', 'h3', 'h4'}:
            name = text(node)
            if specific_name(name) and re.search(r'\b(?:scholarship|fellowship|award|grant)\b', name, re.I):
                break
        if getattr(node, 'name', '') in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} and int(node.name[1]) <= rank:
            break
        if getattr(node, 'name', None):
            result.append(node)
    return result


def matched(lines, pattern):
    return '\n'.join(line for line in lines if re.search(pattern, line, re.I))


def extract_document(main, source_url):
    """Return named records with verbatim, entity-local field evidence."""
    entities = []
    path = urlsplit(source_url).path.lower()
    document_text = text(main)
    h1 = main.find('h1')
    title = text(h1) if h1 else ''

    def add(kind, name, values, evidence):
        if kind != 'overview' and not specific_name(name):
            return
        entities.append({'kind': kind, 'name': clean(name), 'values': {key: clean(value) for key, value in values.items() if clean(value)},
                         'source_url': source_url, 'evidence': evidence})

    # Degree directories: the catalog link supplies the actual course name;
    # adjacent study-level headings supply its level. No generic "Courses" rows.
    department = ''
    department_link = main.select_one('.field--name-field-program-dept a')
    if department_link:
        department = text(department_link)
    for heading in main.find_all(['h2', 'h3', 'h4']):
        if text(heading).lower() == 'department':
            following = heading.find_next(['p', 'a'])
            department = text(following) if following else ''
            break
    if re.search(r'program|degree|catalog|course', path):
        for anchor in main.find_all('a', href=True):
            label = text(anchor)
            offering = anchor.find_parent(class_='program-level-section') is not None
            if not offering and not re.search(r'\b(?:B\.?A\.?|B\.?S\.?|BACS|BSCS|BSN|BSW|BFA|MAcc|M\.?S\.?|M\.?A\.?|MBA|MFA|MPH|Ph\.?D\.?|Ed\.?D\.?|Minor|Certificate)\b', label, re.I):
                continue
            if len(label) > 220 or not specific_name(label) or (not offering and not re.search(r'preview_program|program|catalog|degree|bachelor|master|minor|certificate|phd', anchor['href'], re.I)):
                continue
            name = re.sub(r'\s*\([A-Z0-9-]+\)\s*$', '', label)
            prior = anchor.find_previous(['h2', 'h3', 'h4'])
            level = text(prior) if prior and text(prior).lower() in {'undergraduate', 'graduate', 'combined', 'doctoral'} else ''
            code = re.search(r'\(([A-Z0-9-]+)\)\s*$', label)
            add('academics', name, {'name': name, 'level': level, 'department': department,
                'program_code': code.group(1) if code else ''}, {'name': label, 'department': department, 'level': level})

    # A scholarship is a named heading, with its scope ending at the next peer
    # heading. Eligibility and renewal rules never spill into the following award.
    for heading in main.find_all(['h1', 'h2', 'h3', 'h4']):
        name = text(heading)
        if not specific_name(name) or not re.search(r'\b(?:scholarship|fellowship|award|grant)\b', name, re.I):
            continue
        if re.search(r'appeal|recipients|application|how to|polic|search|scholarships\b|awards\b', name, re.I):
            continue
        nodes = scope_after(heading, stop_at_award=True)
        lines = blocks(nodes)
        if not lines:
            continue
        amount_line = next((line for line in lines if re.search(r'[$£€]\s*\d', line)), '')
        amounts = re.findall(r'[$£€]\s*[\d,]+(?:\.\d{2})?', amount_line)
        duration_line = next((line for line in lines if re.search(r'\b\d+\s+(?:semesters|years|months)\b', line)), '')
        duration = re.search(r'\b\d+\s+(?:semesters|years|months)\b', duration_line)
        renewal = matched(lines, r'renew|maintain')
        eligibility = matched(lines, r'eligible|eligibility|must |required|high school|first.year|transferring|at the time of admission')
        period = re.search(r'(?:each|per) (?:academic )?year|annual(?:ly)?|per semester', amount_line, re.I)
        deadline = matched(lines, r'\bdeadline\b|apply by')
        tables = [text(node) for node in nodes if getattr(node, 'name', '') == 'table']
        gpa_line = next((line for line in lines if re.search(r'GPA', line) and re.search(r'admission|eligible', line) and not re.search(r'renew|maintain', line, re.I)), '')
        gpa = re.search(r'(\d\.\d+)\s*(?:or higher\s*)?(?:GPA|cumulative)', gpa_line, re.I) or re.search(r'GPA of (\d\.\d+)', gpa_line, re.I)
        values = {'name': name, 'amount': amount_line,
            'award_period': period.group(0) if period else '', 'duration': duration.group(0) if duration else '',
            'eligibility': eligibility, 'minimum_gpa': gpa.group(1) if gpa else '',
            'renewal_criteria': renewal, 'renewable': 'Yes' if renewal else '', 'deadline': deadline,
            'award_tiers': '\n'.join(tables), 'notes': '\n'.join(lines[:2])}
        add('scholarships', name, values, {'name': name, 'amount': amount_line, 'duration': duration_line,
            'eligibility': eligibility, 'renewal_criteria': renewal, 'minimum_gpa': gpa_line, 'deadline': deadline})

    if re.search(r'housing|residence|accommodation|dorm', path) and specific_name(title):
        if re.search(r'room|apartment|residence hall|bedroom', document_text, re.I) and not re.search(r'polic|staff|contact|forms|rates|cost|apply|guest|move|guide', title, re.I):
            lines = blocks(main.find_all(['p', 'li']))
            add('housing', title, {'name': title, 'description': '\n'.join(lines[:2]),
                'room_type': matched(lines, r'bedroom|room type|single|double'),
                'amenities': matched(lines, r'kitchen|laundry|furnished|bathroom|internet|amenit'),
                'eligibility': matched(lines, r'eligible|must |first.year|sophomore|graduate')}, {'name': title, 'scope': '\n'.join(lines)})

    # Fee tables keep each row and student category together rather than applying
    # one amount globally to every course or scholarship.
    if re.search(r'tuition|fees|cost|rates', path):
        for table in main.find_all('table'):
            rows = table.find_all('tr')
            if len(rows) < 2:
                continue
            headers = [text(cell) for cell in rows[0].find_all(['th', 'td'])]
            heading = table.find_previous(['h2', 'h3', 'h4'])
            context = text(heading) if heading else title
            for row in rows[1:]:
                cells = [text(cell) for cell in row.find_all(['th', 'td'])]
                if len(cells) < 2 or not cells[0]:
                    continue
                for index, value in enumerate(cells[1:], 1):
                    if not re.search(r'[$£€]\s*\d', value):
                        continue
                    population = headers[index] if index < len(headers) else ''
                    name = ' — '.join(filter(None, [context, cells[0], population]))
                    add('fees', name, {'name': name, 'amount': value, 'applicant_scope': population,
                        'table_context': context, 'charge_label': cells[0],
                        'notes': text(row)}, {'name': context, 'amount': text(row), 'applicant_scope': ' | '.join(headers)})

    # Contact autofill is deliberately conservative: explicit address elements
    # and single contact values on a contact page, with manual profile precedence.
    values = {}
    address = main.find('address')
    contact_page = path.rstrip('/') in {'', '/contact', '/about/contact', '/admissions/contact', '/contact-us'}
    if address and contact_page:
        values['admissions_office_address'] = text(address)
    if contact_page:
        emails = list(dict.fromkeys(a['href'][7:].split('?')[0] for a in main.select('a[href^="mailto:"]')))
        phones = list(dict.fromkeys(text(a) for a in main.select('a[href^="tel:"]')))
        if len(emails) == 1:
            values['contact_email'] = emails[0]
        if len(phones) == 1:
            values['contact_phone'] = phones[0]
    if values:
        add('overview', 'University contact information', values, values.copy())
    return entities


@transaction.atomic
def save_entities(university_id, entities):
    from universities.models import University
    from django_api.models import UniversityKnowledgeEntry
    university = University.objects.select_for_update().get(uuid=university_id)
    count = 0
    for entity in entities:
        kind, name = entity['kind'], entity['name']
        key = identity(kind, name)
        row = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), details___information_entity_key=key).first()
        if row is None:
            row = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid),
                details__information_type=kind, details__name__iexact=name,
                source_type__in=['manual', 'human_verified']).first()
        if row and row.source_type in {'manual', 'human_verified'}:
            continue
        if row is None:
            row = UniversityKnowledgeEntry(university_id=str(university.uuid), topic=name, source_type='scraped', confidence=0.95)
        previous = dict(row.details or {})
        snapshots = dict(previous.get('_information_source_values', {}))
        snapshots[entity['source_url']] = entity['values']
        values = {'information_type': kind}
        multi = {'eligibility', 'renewal_criteria', 'notes', 'award_tiers', 'amount', 'deadline'}
        for fields in snapshots.values():
            for field, value in fields.items():
                if field not in values:
                    values[field] = value
                elif field in multi and value not in values[field]:
                    values[field] += '\n\n' + value
        sources = dict(previous.get('_information_field_sources', {}))
        for field, value in entity['values'].items():
            sources[field] = {'url': entity['source_url'], 'quote': entity['evidence'].get(field, entity['evidence'].get('scope', value))}
        row.details = {**values, '_information_entity_key': key, '_information_source_values': snapshots,
            '_information_category': 'campus' if kind == 'housing' else kind,
            '_information_field_sources': sources, '_information_extracted_at': timezone.now().isoformat()}
        row.content = '\n'.join(f"{field.replace('_', ' ').title()}: {value}" for field, value in values.items() if value and field != 'information_type')
        row.source_url = entity['source_url']
        row.save()
        count += 1
    return count


def ingest_document(university_id, document, source_url):
    return save_entities(university_id, extract_document(document, source_url))
