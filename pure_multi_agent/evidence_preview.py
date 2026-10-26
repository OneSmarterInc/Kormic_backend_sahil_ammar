"""Question-focused excerpts; complete tool results remain separately pageable."""
import json
import re


STOP = frozenset('a an the is are was were what how who does do can for of in to '
                 'and or with their our this that have has at be it university'.split())


def terms(text):
    return {word.rstrip('s') for word in re.findall(r'[a-z0-9]+', text.lower())
            if word not in STOP and len(word) > 1}


def focused_preview(content, question, limit):
    """Return exact source slices, with IDs/URLs and explicit omissions."""
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get('records'), list):
        return None
    wanted = terms(question)
    if not wanted:
        return None
    candidates = []
    for index, record in enumerate(payload['records']):
        if not isinstance(record, dict) or not isinstance(record.get('content'), str):
            continue
        text = record['content']
        best = None
        # Overlap keeps adjacent headings and table values together. Never
        # rewrite amounts or treat an excerpt as the complete source document.
        for start in range(0, max(1, len(text)), 600):
            excerpt = text[start:start + 1800]
            score = len(wanted & terms(excerpt))
            if best is None or score > best[0]:
                best = (score, start, excerpt)
        if best and best[0]:
            score, start, excerpt = best
            item = {key: record.get(key) for key in ('id', 'topic', 'source_url', 'source_type')}
            item.update(content=excerpt, excerpt_offset=start,
                        original_characters=len(text), partial=start > 0 or len(excerpt) < len(text))
            candidates.append((score, index, item))
    selected = []
    for _, _, item in sorted(candidates, key=lambda row: (-row[0], row[1])):
        trial = json.dumps({'records': [*selected, item], 'partial': True}, ensure_ascii=False)
        if len(trial) <= limit:
            selected.append(item)
        if len(selected) == 2:
            break
    return json.dumps({'records': selected, 'partial': True}, ensure_ascii=False) if selected else None
