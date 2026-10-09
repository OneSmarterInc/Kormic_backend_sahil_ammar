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


def compact_evidence(data, question):
    """Preserve whole records, prioritise the requested subject, bound prompt size."""
    result = dict(data)
    words = set(re.findall(r'\w{4,}', question.lower())) - {'what', 'which', 'university', 'please', 'about', 'tell'}
    def score(item):
        text = json.dumps(item, default=str).lower()
        return sum(word in text for word in words)
    for key, limit in (('facts', 5), ('courses', 6), ('intakes', 6), ('saved_knowledge', 4), ('previous_answers_for_this_student', 2)):
        values = data.get(key, [])
        if isinstance(values, list):
            ranked = sorted(unique_records(values), key=score, reverse=True)
            kept, size = [], 0
            for value in ranked[:limit]:
                length = len(json.dumps(value, default=str))
                if size + length > 12000:
                    continue
                kept.append(value); size += length
            result[key] = kept
    # References carry identity, not an entire duplicated catalogue.
    if isinstance(result.get('university'), dict):
        result['university'] = {k:v for k,v in result['university'].items() if k != 'coverage'}
    return result


def requested_topic_missing(data, question):
    groups = ((r'fees?|tuition|cost', ('tuition','fees','cost')),
              (r'deadline|intake', ('deadline','intake')),
              (r'requirements?|eligib', ('requirements','eligibility','admission')),
              (r'scholarship|funding', ('scholarship','funding')))
    for pattern, labels in groups:
        if not re.search(pattern, question, re.I):
            continue
        records = data.get('facts', []) + data.get('courses', []) + data.get('intakes', [])
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
    fields = [('tuition', 'Tuition'), ('currency', 'Currency'), ('academic_year', 'Academic year'), ('requirements', 'Requirements')]
    for course in courses[:3]:
        details = [f'{label}: {course[key]}' for key,label in fields if course.get(key) and str(course[key]).lower() not in ('n/a','null','none','unknown')]
        if details:
            lines.append('**' + course['name'] + '**\n' + '\n'.join('- ' + item for item in details))
    if lines:
        return '\n\n'.join(lines) + '\n\nThese are the saved programme details. I could not complete the rest of your answer; dates and amounts apply only to the academic year shown.'
    facts = evidence.get('facts', []) if isinstance(evidence, dict) else []
    statements = [fact['content'].strip() for fact in facts if isinstance(fact, dict)
                  and isinstance(fact.get('content'), str) and len(fact['content']) < 600
                  and not problems(fact['content'])]
    if statements:
        return '\n\n'.join(statements[:3]) + '\n\nI could not complete the remaining requested details.'
    return 'I could not finish preparing this answer. Please retry your question; your conversation is still available.'
