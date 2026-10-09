"""Place one cost record with its owner without copying amounts into other facts."""
import re


def normalized(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()


def place_costs(records):
    targets = [r for r in records if r['details'].get('information_type') in {'academics', 'housing'}]
    by_id = {str(r['id']): r for r in targets}
    for record in records:
        if record['details'].get('information_type') != 'fees':
            continue
        owner = record['details'].get('cost_owner', '')
        placement = {'scope': 'review', 'target_id': None}
        if owner == 'university':
            placement['scope'] = 'university'
        elif ':' in owner:
            kind, target_id = owner.split(':', 1)
            target = by_id.get(target_id)
            if target and target['details'].get('information_type') == kind:
                placement = {'scope': kind, 'target_id': target['id']}
        elif not owner:
            details = record['details']
            evidence = normalized(' '.join(str(details.get(key, '')) for key in ['name', 'program', 'table_context', 'charge_label']))
            matches = []
            for target in targets:
                kind = target['details']['information_type']
                if kind == 'academics' and re.search(r'\b(programs|college|school|department)\b', evidence):
                    # A college-wide M.S. rate is not evidence for a single M.S.
                    # program whose title happens to be a substring of it.
                    continue
                name = target['details'].get('name') or target['topic']
                aliases = [normalized(name)]
                code = target['details'].get('program_code')
                if code:
                    aliases.append(normalized(code))
                if kind == 'housing':
                    alias = re.sub(r'\b(the|apartments|hall|community)\b', '', normalized(name))
                    alias = ' '.join(alias.split())
                    if len(alias) >= 5:
                        aliases.append(alias)
                    if re.search(r'tuition|fees|services|housing options|residence halls|^apartments$', name, re.I):
                        continue
                if any(len(alias) >= 5 and f' {alias} ' in f' {evidence} ' for alias in aliases):
                    matches.append(target)
            if len(matches) == 1:
                target = matches[0]
                placement = {'scope': target['details']['information_type'], 'target_id': target['id']}
            elif not matches:
                # Use the charge label, not a tuition-page heading, to identify
                # common fees. Unknown tuition/budgets remain available to review.
                parts = record['topic'].split(' — ')
                label = details.get('charge_label') or (parts[1] if len(parts) > 1 else record['topic'])
                if re.search(r'\b(application fee|registration fee|transcript|late fee|late payment|payment plan|campus engagement fee|legal services fee|international student fee|graduation fee)\b', label, re.I):
                    placement['scope'] = 'university'
        record['cost_placement'] = placement
    return records


def validate_cost_owner(details, university_id):
    """Return a readable ownership label, or reject cross-university links."""
    from django_api.models import UniversityKnowledgeEntry
    owner = details.get('cost_owner', '')
    if not isinstance(owner, str):
        raise ValueError('Choose a valid cost owner.')
    if owner in {'', 'review'}:
        return 'Needs assignment review'
    if owner == 'university':
        return 'University-wide fee'
    kind, separator, target_id = owner.partition(':')
    if not separator or kind not in {'academics', 'housing'} or not target_id.isdigit():
        raise ValueError('Choose a valid program or housing option for this cost.')
    target = UniversityKnowledgeEntry.objects.filter(pk=target_id, university_id=university_id,
        details__information_type=kind).first()
    if not target:
        raise ValueError('This program or housing option is unavailable.')
    return target.topic
