"""Keep financial rates attached to their source period, never their fetch date."""
import re
from datetime import date


PERIOD = re.compile(r'\b(20\d{2})\s*[–—/-]\s*(20\d{2}|\d{2})(?!\d)|'
                    r'\b(Fall|Autumn|Spring|Summer|Winter)\s+(20\d{2})\b', re.I)
DEPOSIT_ROW = re.compile(
    r'\b(?:F[-‑]1\s+International\s+)?(?:New[- ]Student|Tuition)\s+Deposit'
    r'\s*\([^)]{1,500}\)\s*[$€£]\s*\d[\d,.]*', re.I)


def normalize_period(text: str) -> str:
    text = re.sub(r'\s*[-–—/]\s*', '-', text).casefold()
    return re.sub(r'\b(20\d{2})-(\d{2})\b', lambda match: match[1] + '-20' + match[2], text)


def deposit_table_excerpt(text: str, question: str, budget: int) -> str:
    """Keep complete deposit table rows with their actual preceding headings."""
    if not re.search(r'new[- ]student\s+deposit', question, re.I):
        return ''
    rows = []
    for match in DEPOSIT_ROW.finditer(text):
        periods = list(PERIOD.finditer(text[:match.start()]))
        period = periods[-1][0] if periods else ''
        content = (period + '\n' if period else '') + match[0]
        assessment = financial_period_context(question, {'facts': [{'content': content}]})
        current = bool(assessment and not assessment['requires_confirmation'])
        rows.append((current, normalize_period(period), content))
    kept, size = [], 0
    for _, _, content in sorted(set(rows), reverse=True):
        if size + len(content) > budget:
            continue
        kept.append(content)
        size += len(content)
        if len(kept) == 2:
            break
    return '\n\n'.join(kept)


def financial_quote(passage: str, topic: str) -> str:
    if 'deposit' in topic:
        row = DEPOSIT_ROW.search(passage)
        if row:
            return row[0]
    # Never expose a whole flattened fee table as a sentence. When a complete
    # short passage cannot be isolated, leave it for the normal answer path.
    return next((part.strip() for part in re.split(r'\n+|(?<=[.!?])\s+(?=[A-Z])', passage)
                 if re.search(topic, part, re.I)
                 and re.search(r'[$€£]\s*\d|\b\d[\d,]*\s*(?:USD|dollars)\b', part, re.I)
                 and len(part.strip()) <= 450), '')


def financial_period_context(question: str, evidence: dict, *, today=None) -> dict:
    """Conservative currency check; a recent fetch never updates an old fee.

    An undated rate or ambiguous applicability requires officer confirmation.
    A year range is accepted for an unspecified current query in autumn of
    its first year; otherwise term-boundary ambiguity requires confirmation.
    Explicit requested academic years/semesters are matched exactly instead.
    """
    today = today or date.today()
    deposit = bool(re.search(r'\bdeposits?\b', question, re.I))
    if not deposit and not re.search(r'\b(?:tuition|fees?|costs?|per.credit)\b', question, re.I):
        return {}
    wanted = list(PERIOD.finditer(question))

    def identity(match):
        if match[1]:
            start = int(match[1])
            end = int(match[2])
            return ('year', start, end if end >= 100 else start // 100 * 100 + end)
        return ('term', match[3].lower().replace('autumn', 'fall'), int(match[4]))

    target = {identity(match) for match in wanted}
    topic = r'\bdeposits?\b' if deposit else r'\b(?:tuition|fees?|per.credit)\b'
    if deposit and re.search(r'new[- ]student', question, re.I):
        topic = r'\b(?:new[- ]student\s+deposit|tuition\s+deposit)\b'
    records = []

    def visit(data):
        if not isinstance(data, dict):
            return
        for key in ('facts', 'saved_knowledge', 'previous_answers_for_this_student'):
            for fact in data.get(key, []):
                text = str(fact.get('content') or fact.get('source_quote') or fact.get('answer') or '')
                if fact.get('academic_year'):
                    text = str(fact['academic_year']) + '\n' + text
                # Period headings scope the following table; dates in other
                # tables and crawl timestamps are not evidence for this rate.
                periods = list(PERIOD.finditer(text))
                spans = [(None, text[:periods[0].start()] if periods else text)]
                spans += [(match, text[match.end():periods[i + 1].start() if i + 1 < len(periods) else len(text)])
                          for i, match in enumerate(periods)]
                for period, passage in spans:
                    if period:
                        prefix = re.split(r'[\n.!?]', text[:period.start()])[-1][-100:]
                        if not re.search(r'[$€£]|\b20\d{2}\b', prefix):
                            passage = prefix + ' ' + passage
                    if not re.search(topic, passage, re.I) or not re.search(r'[$€£]\s*\d|\b\d[\d,]*\s*(?:USD|dollars)\b', passage, re.I):
                        continue
                    period_id = identity(period) if period else None
                    if target:
                        current = period_id in target
                    elif period_id and period_id[0] == 'year':
                        current = (today.month >= 9 and period_id[1] == today.year
                                   and period_id[2] == today.year + 1)
                    elif period_id:
                        term = 'spring' if today.month <= 5 else 'summer' if today.month <= 8 else 'fall'
                        current = period_id == ('term', term, today.year)
                    else:
                        current = False
                    records.append({'period': period[0] if period else 'undated',
                                    'matches_requested_period': current,
                                    'source_url': fact.get('source_url', ''),
                                    'quote': financial_quote(passage, topic)})
        visit(data.get('official_website_research'))

    visit(evidence)
    if not records:
        return {}
    return {'rates': records, 'requires_confirmation': not any(r['matches_requested_period'] for r in records),
            'instruction': 'Use the rate only for its stated source academic year or semester. '
            'Prefer matching current/requested-period evidence. Otherwise label dated rates as historical '
            '(or not confirmed for the requested period), and undated rates as unverified. '
            'Show the supported amount and citation as reference, not a current charge. '
            'The university must confirm the current/requested rate. Never use a fetch date as the rate year.'}


def financial_reference_answer(assessment: dict) -> str:
    """Recover only literal cited rate passages, never a rejected model draft."""
    if not assessment.get('requires_confirmation'):
        return ''
    references = []
    seen = set()
    for rate in sorted(assessment.get('rates', []), key=lambda row: (row['period'] != 'undated', row['period']), reverse=True):
        quote, url, period = rate.get('quote'), rate.get('source_url'), rate['period']
        if not quote or not url or (quote, url, period) in seen:
            continue
        seen.add((quote, url, period))
        # Preserve the source wording as quotation, not generated advice.
        quote = re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', quote)
        label = 'Published deposit — year not specified in the saved excerpt' if period == 'undated' else f'Published for {period} — not confirmed for the requested period'
        references.append(f'**{label}**\n\n> {quote}\n\nSource: {url}')
        if len(references) == 1:
            break
    if not references:
        return ''
    return ('I cannot confirm the current deposit or fee from the available records. '
            'This published rate is reference information, not a verified current charge:\n\n'
            + '\n\n'.join(references)
            + '\n\nThe university needs to confirm the applicable amount and payment conditions.')
