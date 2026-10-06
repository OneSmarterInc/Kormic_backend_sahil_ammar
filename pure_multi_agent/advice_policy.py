"""Shared constraints for student and university advice."""
import json
import re


def budget_context(profile):
    """Preserve explicit units; a legacy numeric budget has no implied currency."""
    text = str(profile.get('budget_text') or '')
    currencies = re.findall(r'\b(INR|USD|GBP|EUR|CAD|AUD)\b', text.upper())
    if '₹' in text:
        currencies.append('INR')
    currency = currencies[0] if len(set(currencies)) == 1 else None
    period = ('entire_program' if re.search(r'\b(total|entire|whole)\b', text, re.I)
              else 'annual' if re.search(r'\b(annual|per year|a year|yearly)\b', text, re.I) else None)
    living = (True if re.search(r'\b(including|includes|with)\b.*\bliving\b', text, re.I)
              else False if re.search(r'\b(tuition.only|excluding living|without living)\b', text, re.I) else None)
    result = {'amount': profile.get('budget'), 'currency': currency, 'period': period,
            'includes_living': living,
            'student_description': text or None,
            'rule': 'Never assume USD or annual units. Unknown units require clarification. Do not infer affordability from tuition alone or invent exchange rates.'}
    result.update(profile.get('_conversation_budget', {}))
    return result


ADVICE_RULES = """
Answer the CURRENT question first. General explanations (planning, IELTS versus TOEFL,
GATE alternatives, APS, documents, recommenders, offer versus visa) do not need a university
identity lookup. Do not turn a country or degree-type comparison into a pick-one university menu.
An offer can be conditional or unconditional. A visa and permission to enter are distinct.
Institution-specific requirements, dates, fees and scholarships must come from relevant
official evidence for the exact programme, level, applicant category and intake. Missing
evidence means unknown, not permission to fill gaps from memory. Old deadlines are historical.
Missing evidence does not establish that an institution has not published a detail.
Do not turn a historical deadline into a future-intake deadline or say an announcement
has not happened merely because retrieval did not find it. GRE and English-test policies
are programme-specific; never make universal test requirements or promise funding.
Do not describe model-generated text or search snippets as verified official information.
Compare every requested institution on the same criteria. If a requested shortlist cannot
be completed, say exactly how many supported options were found. Funding and housing are
not universities. Never invent relative prestige, selectivity, facilities or placement rankings.
Give a provisional useful plan under clearly stated assumptions instead of blocking on
optional preferences. Ask at most one material follow-up after providing the requested help.
Saved skills are self-reported, not demonstrated proficiency. Missing fields do not prove
missing experience. Only use truthful achievements and metrics in drafts; use placeholders
for unknowns. An internship and no full-time employment are compatible facts.
The saved program/major describe the student's current or previous education, not the
target degree abroad. Never infer the intended degree from those fields. Use the requested
target programme from the conversation; if absent, say target programme rather than BTech.
Never claim to have read a resume unless actual resume evidence has been retrieved. A profile
review is not a document review. For GitHub connection: open Profile, choose Connect GitHub,
and authorize the GitHub OAuth connection. Never invent a public/private profile setting or
tell students to request account access from Kormic. Do not expose tool names or exceptions.
"""


def advice_context(profile):
    return ADVICE_RULES + '\nAUTHORITATIVE BUDGET (data): ' + json.dumps(budget_context(profile), default=str)


def budget_clarification(text):
    """Only explicit first-person budget statements; never assistant guesses."""
    if not re.search(r"\b(?:my budget|budget is|budget:)\b", text, re.I) or '?' in text:
        return {}
    data = {}
    codes = set(re.findall(r'\b(INR|USD|GBP|EUR|CAD|AUD)\b', text.upper()))
    if len(codes) == 1:
        data['currency'] = codes.pop()
    if re.search(r'\b(annual|per year|yearly|a year)\b', text, re.I):
        data['period'] = 'annual'
    elif re.search(r'\b(total|entire program|whole program)\b', text, re.I):
        data['period'] = 'entire_program'
    numbers = re.findall(r'(?:\b(?:USD|INR|GBP|EUR|CAD|AUD)\s*|\bmy budget\s*(?:is|:)\s*)(\d[\d,]*(?:\.\d+)?)', text, re.I)
    if len(numbers) == 1:
        data['amount'] = float(numbers[0].replace(',', ''))
    if re.search(r'\b(including|includes|with)\b.*\bliving\b', text, re.I):
        data['includes_living'] = True
    elif re.search(r'\b(tuition.only|excluding living|without living)\b', text, re.I):
        data['includes_living'] = False
    return data


def apply_budget_clarifications(profile, statements):
    values = {}
    for statement in statements:
        values.update(budget_clarification(statement))
    return {**profile, '_conversation_budget': values} if values else profile


def load_budget_clarifications(ctx):
    from django_api.models import ChatMessage
    rows = ChatMessage.objects.filter(student_id=ctx['canonical_student_id'], sender='user')
    if ctx.get('current_message_id'):
        rows = rows.filter(id__lt=ctx['current_message_id'])
    statements = list(rows.order_by('-id').values_list('content', flat=True)[:30])[::-1]
    statements.append(ctx['current_message'])
    ctx['student_profile'] = apply_budget_clarifications(ctx['student_profile'], statements)
    ctx['budget_clarification_turn'] = bool(budget_clarification(ctx['current_message']))
