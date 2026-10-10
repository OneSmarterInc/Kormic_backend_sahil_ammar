"""Bounded, question-specific model input and non-model failure recovery."""
import json
import re
from pure_multi_agent.chat_cost_controls import unique_records


def study_focus(previous, text):
    result = dict(previous or {})
    # These describe the requested study, never overwrite the student's qualification.
    degree = re.search(r'\b(MS|MSc|MTech|PhD|masters?|master[’\']s|bachelors?|bachelor[’\']s)\b', text, re.I)
    if degree and not re.search(r'\b(my|current|after my|after)\s*$', text[:degree.start()], re.I):
        result['target_degree'] = degree[0]
    subject = re.search(r'\b(computer science|artificial intelligence|data science|informatics|software engineering)\b', text, re.I)
    if subject:
        result['target_subject'] = subject[0]
    intake = re.search(r'\b(?:fall|spring|summer|autumn|winter|september|january)\s+20\d{2}\b', text, re.I)
    if intake:
        result['target_intake'] = intake[0]
    return result


def profile_context(profile, question=''):
    keys = ('name', 'major', 'program', 'graduation_year', 'career_goals')
    if re.search(r'budget|cost|fees?|afford|fund|loan', question, re.I):
        keys += ('budget', 'budget_text', '_conversation_budget')
    if re.search(r'visa|citizen|nationality|international|profile|fit|eligib|requirements', question, re.I):
        keys += ('country',)
    result = {k: profile[k] for k in keys if profile.get(k) not in (None, '', [], {})}
    if re.search(r'profile|fit|eligible|eligibility|requirements|career|resume|skills', question, re.I):
        for key in ('gpa', 'gpa_scale', 'skills', 'technical_skills', 'projects', 'work_months', 'ielts', 'toefl', 'gre_quant', 'gre_verbal'):
            if profile.get(key) not in (None, '', [], {}):
                result[key] = profile[key][:15] if isinstance(profile[key], list) else profile[key]
    return result


def evidence_excerpt(text, question, budget=3500):
    """Select source passages with neighbouring qualifications, retaining order."""
    if len(text) <= budget:
        return text
    from university_research.relevance import question_terms
    terms = question_terms(question)
    parts = re.split(r'\n\s*\n|(?<=[.!?])\s+(?=[A-Z])', text)
    ranked = sorted(range(len(parts)), key=lambda i: sum(t in parts[i].lower() for t in terms), reverse=True)
    selected, used = set(), 0
    for index in ranked:
        for candidate in (index, index - 1, index + 1):
            if candidate < 0 or candidate >= len(parts) or candidate in selected:
                continue
            if used + len(parts[candidate]) <= budget:
                selected.add(candidate); used += len(parts[candidate])
    if selected:
        return '\n[…]\n'.join(parts[i] for i in sorted(selected))
    # A single enormous paragraph still supplies an explicitly partial excerpt.
    matches = [m.start() for term in terms for m in re.finditer(re.escape(term), text.casefold())]
    start = max(0, min(matches) - 300) if matches else 0
    return ('[…] ' if start else '') + text[start:start + budget].rsplit(' ', 1)[0] + ' […]'


def compact_evidence(data, question):
    """Preserve whole records, prioritise the requested subject, bound prompt size."""
    result = dict(data)
    from university_research.relevance import question_terms
    words = set(question_terms(question))
    def score(item):
        text = json.dumps(item, default=str).lower()
        return sum(word in text for word in words)
    for key, limit in (('facts', 5), ('courses', 6), ('intakes', 6), ('saved_knowledge', 4), ('previous_answers_for_this_student', 2)):
        values = data.get(key, [])
        if isinstance(values, list):
            ranked = sorted(unique_records(values), key=score, reverse=True)
            kept, size = [], 0
            for value in ranked[:limit]:
                if isinstance(value, dict) and len(json.dumps(value, default=str)) > 6000:
                    value = dict(value)
                    for field in ('content', 'source_quote', 'requirements', 'description'):
                        if isinstance(value.get(field), str):
                            value[field] = evidence_excerpt(value[field], question, 2200)
                    value['evidence_partial'] = True
                    value['excerpt_notice'] = 'Selected source excerpts; omitted text may contain additional conditions. Do not call these complete admission requirements.'
                length = len(json.dumps(value, default=str))
                if size + length > 12000:
                    continue
                kept.append(value); size += length
            result[key] = kept
    # References carry identity, not an entire duplicated catalogue.
    if isinstance(result.get('university'), dict):
        result['university'] = {k:v for k,v in result['university'].items() if k != 'coverage'}
    if isinstance(result.get('official_website_research'), dict):
        result['official_website_research'] = compact_evidence(result['official_website_research'], question)
    return result


def requested_topic_missing(data, question):
    groups = ((r'fees?|tuition|cost', ('tuition','fees','cost')),
              (r'deadline|intake', ('deadline','intake')),
              (r'admission|requirements?|eligib', ('requirements','eligibility','admission')),
              (r'scholarship|funding', ('scholarship','funding')))
    for pattern, labels in groups:
        if not re.search(pattern, question, re.I):
            continue
        records = data.get('facts', []) + data.get('courses', []) + data.get('intakes', []) + data.get('saved_knowledge', [])
        covered = False
        for item in records:
            for key, value in item.items():
                if any(label in key.lower() for label in labels) and value and str(value).lower() not in ('n/a','null','unknown'):
                    covered = True
            if any(label in (item.get('topic','') + ' ' + item.get('content','')).lower() for label in labels):
                covered = True
        if not covered:
            return True
    return False


def validation_evidence(messages, ctx):
    evidence = [m.content for m in messages if m.type == 'tool']
    if ctx.get('turn_intent', {}).get('route') == 'general':
        evidence.append({'student_statements':[m.content for m in messages if m.type == 'human'],
                         'target_study':ctx.get('study_focus', {})})
    return evidence


def partial_answer(question, evidence, draft='', profile=None):
    """Only on generation failure: retain supported prose or saved structured facts."""
    from pure_multi_agent.answer_checks import violations
    from pure_multi_agent.response_contract import problems
    # Never salvage fragments of a broken answer: that can detach a claim from
    # its qualification or leave instructions referring to a removed deadline.
    if isinstance(draft, str) and draft.strip() and not problems(draft) and not violations(draft, evidence, profile):
        return draft.strip()
    courses = evidence.get('courses', []) if isinstance(evidence, dict) else []
    lines = []
    admission_question = bool(re.search(r'admission|requirements?|eligib', question, re.I))
    fields = ([('level', 'Degree level'), ('academic_year', 'Academic year'), ('requirements', 'Requirements')]
              if admission_question else [('tuition', 'Tuition'), ('currency', 'Currency'), ('academic_year', 'Academic year'), ('requirements', 'Requirements')])
    for course in courses[:3]:
        details = [f'{label}: {course[key]}' for key,label in fields if course.get(key) and str(course[key]).lower() not in ('n/a','null','none','unknown')]
        if details:
            source = course.get('page__url') or course.get('source_url')
            link = '\nSource: ' + source if isinstance(source, str) and source.startswith(('https://', 'http://')) else ''
            lines.append('**' + course['name'] + '**\n' + '\n'.join('- ' + item for item in details) + link)
    if lines:
        if admission_question:
            return '\n\n'.join(lines) + '\n\nThese are partial saved requirements for the programmes named above, not a complete university-wide checklist. Requirements may vary by programme, applicant type and intake; confirm missing conditions with the linked source.'
        return '\n\n'.join(lines) + '\n\nThese are the saved programme details. I could not complete the rest of your answer; dates and amounts apply only to the academic year shown.'
    facts = evidence.get('facts', []) + evidence.get('saved_knowledge', []) if isinstance(evidence, dict) else []
    statements = []
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get('content'), str) or not fact['content'].strip():
            continue
        content = evidence_excerpt(fact['content'].strip(), question, 1400)
        if problems(content):
            continue
        source = fact.get('source_url')
        link = '\nSource: ' + source if isinstance(source, str) and source.startswith(('https://', 'http://')) else ''
        statements.append('Saved source excerpt:\n' + content + link)
    if statements:
        return '\n\n'.join(statements[:3]) + '\n\nThese excerpts are partial, not a complete requirements checklist. I could not complete the remaining requested details.'
    return 'I could not finish preparing this answer. Please retry your question; your conversation is still available.'
