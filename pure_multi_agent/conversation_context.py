"""Small, task-relevant consultation history; full history stays in the DB."""

import re


_OLDER = re.compile(r'\b(?:earlier|previous(?:ly)?|last time|as discussed|as I said|you said|remember)\b', re.I)
_DECISION = re.compile(r'\b(?:taught|research|thesis|masters?|phd|intake|international|domestic|citizen|visa)\b', re.I)


def needs_history_lookup(question):
    return bool(_OLDER.search(question))


def relevant_history(rows, question, focus=None, limit=4):
    """Keep recent context and older matching clarifications in original order."""
    if not rows:
        return []
    terms = set(re.findall(r'\w{4,}', question.casefold()))
    terms.update(re.findall(r'\w{4,}', ' '.join(str(v) for v in (focus or {}).values()).casefold()))
    candidates = []
    for index, row in enumerate(rows):
        content = str(row.get('content', ''))
        tokens = set(re.findall(r'\w{4,}', content.casefold()))
        score = len(terms & tokens) * 3 + (2 if _DECISION.search(content) else 0)
        # A recent exchange remains available even if vocabulary changed.
        if index >= len(rows) - 2:
            score += 5
        candidates.append((score, index, row))
    chosen = sorted(sorted(candidates, key=lambda item: (-item[0], -item[1]))[:limit], key=lambda item: item[1])
    return [{'actor': row.get('actor') or row.get('speaker'),
             'content': str(row.get('content', ''))[:500]}
            for _, _, row in chosen]
