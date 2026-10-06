"""Reviewed help content for stable concepts and actual product operations.

Reviewed 2026-09-30. Links, not stored dates/fees, determine current eligibility.
Only used for a matching stand-alone question; never as university evidence.
"""

ANSWERS = {
    'german_language': (
        'Do not assume one rule for every English-taught programme. Some do not require German '
        'for admission, while individual programmes may specify German-language study or other '
        'requirements during the degree. Check the exact programme’s admissions page and regulations. '
        'Learning German is also useful for housing, everyday administration and many jobs. '
        'Which programme are you considering?'),
    'german_cost': (
        'Not universally. Some public programmes charge little or no tuition, but fees can depend '
        'on the state, university, programme and applicant category. For example, '
        '[TUM publishes tuition and eligibility information for its Informatics MSc]'
        '(https://www.tum.de/en/studies/degree-programs/detail/informatics-master-of-science-msc). '
        'Semester contributions, housing, food, insurance, travel and other living costs still matter '
        'even when tuition is zero. To assess your budget, compare the official fees and living costs '
        'for the whole degree; do not assume Germany is free or that a particular budget is sufficient.'),
    'aps': (
        'APS India is the Academic Evaluation Center. It verifies Indian academic documents '
        'for study in Germany; it is not a university course or scholarship. Its certificate '
        'is generally part of the German student-visa documentation, but the applicable '
        'procedure depends on your qualifications and circumstances. Check the '
        '[official APS requirements and eligibility checker](https://aps-india.de/) for your '
        'case and intake, including any additional tests. APS does not itself grant university admission or a visa.'),
    'english_tests': (
        'Choose IELTS Academic or TOEFL iBT based on the accepted tests and minimum scores '
        'of every programme on your shortlist, then compare the test format, available dates '
        'and fees. Do not choose solely by country: TOEFL is accepted in many countries, '
        'including the UK and US, and acceptance is programme-specific. Check the '
        '[TOEFL institution information](https://www.ets.org/toefl/test-takers/ibt/where-to-study.html) '
        'and each programme’s official admissions page. Try sample questions from both before booking. '
        'Which programmes are you considering?'),
    'gate': (
        'No—GATE is not compulsory for every MTech programme or admission category. '
        'Check the exact programme’s rules. For example, [IIIT Hyderabad publishes its MTech '
        'PGEE admission process](https://pgadmissions.iiit.ac.in/m-tech-program/), while '
        '[IIT Bombay publishes GATE and category-specific admission routes](https://acad.iitb.ac.in/admissions/masters/mtech). '
        'If your GATE score is low, investigate institution entrance exams and eligible sponsored '
        'or self-funded routes, compare total costs, or plan a retake. None guarantees admission; '
        'verify the current intake’s eligibility and deadlines.'),
    'offers_visas': (
        'An admission offer is a university’s decision to offer you a place. It can be conditional '
        '(you still need to meet stated requirements) or unconditional. A student visa is a '
        'separate immigration decision; admission does not guarantee a visa. Visa rules depend '
        'on the destination. For example, a US visa allows travel to request entry, but '
        '[does not guarantee admission at the border](https://travel.state.gov/content/travel/en/us-visas/visa-information-resources/visa-expiration-date.html).'),
    'github_connection': (
        'In Kormic, open your Profile and select **Connect GitHub**. Sign in to GitHub if prompted '
        'and authorize the OAuth connection, then return to Kormic. This connects your own account; '
        'you do not need to make your profile public or request account access from Kormic. '
        'Repository analysis is available after the account is connected and its processing completes.'),
}


def reference_reply(ctx, intent):
    import re
    topic = intent.get('reference_topic', 'none')
    text = ctx.get('current_message', '')
    if re.search(r'\b(plan|schedule|weeks?|months?|draft|write|prepare|practice)\b', text, re.I):
        return None
    # Compose reviewed concepts when all requested topics are represented.
    if not intent.get('institutions') and not ctx.get('chat_attachments') and intent.get('route') == 'general':
        topics = []
        if re.search(r'English.taught.*German|German.*English.taught', text, re.I):
            topics.append(('German language', 'german_language'))
        if re.search(r'\bAPS\b', text, re.I):
            topics.append(('APS', 'aps'))
        if re.search(r'\bIELTS\b', text, re.I) and re.search(r'\bTOEFL\b', text, re.I):
            topics.append(('IELTS or TOEFL', 'english_tests'))
        if re.search(r'\bGermany\b', text, re.I) and re.search(r'tuition.free|living.cost', text, re.I):
            topics.append(('Living costs', 'german_cost'))
        if len(topics) >= 2:
            return '\n\n'.join('**' + label + '**\n' + ANSWERS[key].replace('Which programme are you considering?', '').replace('Which programmes are you considering?', '').strip() for label, key in topics)
    # A canned single-topic paragraph must never consume a multi-part request.
    if re.search(r'\balso\b|\bAPS\b.*\b(?:IELTS|TOEFL)\b|\b(?:IELTS|TOEFL)\b.*\bAPS\b', text, re.I) or text.count('?') > 1:
        return None
    if re.search(r'\bGerman\b', text, re.I) and re.search(r'English.taught', text, re.I) and re.search(r'\b(need|know|learn|language)\b', text, re.I):
        topic = 'german_language'
    elif re.search(r'\bGermany\b', text, re.I) and re.search(r'\bfree\b', text, re.I):
        topic = 'german_cost'
    anchors = {'aps': r'\bAPS\b', 'gate': r'\bGATE\b',
               'english_tests': r'\b(IELTS|TOEFL)\b', 'offers_visas': r'\bvisa\b',
               'github_connection': r'\bGitHub\b', 'german_language': r'\bGerman\b', 'german_cost': r'\bGermany\b'}
    if topic not in anchors or not re.search(anchors[topic], text, re.I):
        return None
    if not intent.get('institutions') and not ctx.get('chat_attachments'):
        return ANSWERS.get(topic)
    return None


def missing_resume_reply(ctx):
    """Do not ask a model to critique a document that does not exist."""
    import re
    text = ctx.get('current_message', '')
    if re.search(r'what (?:do )?you know|saved profile|from my profile|using my profile', text, re.I):
        return None
    if not re.search(r'\b(resume|cv)\b', text, re.I) or not re.search(r'\b(uploaded|review|weakness\w*|critique|analyse|analyze)\b', text, re.I):
        return None
    from django_api.models import ResumeUpload
    from pure_multi_agent.document_evidence import manifest
    if ctx.get('canonical_student_id') and not ResumeUpload.objects.filter(student__uuid=ctx['canonical_student_id']).exists() and not manifest(ctx['canonical_student_id']):
        return ('I don’t have an uploaded resume to review, so I can’t identify weaknesses in it. '
                'Upload the resume and I can assess its structure, evidence of achievements and relevance '
                'to your target programme. I can also give general advice from your saved profile, '
                'but that would not be a review of the document.')
    return None


def planning_reply(ctx):
    """Calendar milestones are calculated, not guessed academic deadlines."""
    import re
    import calendar
    from django.utils import timezone
    text = ctx.get('current_message', '')
    if not re.search(r'month[ -]by[ -]month', text, re.I):
        return None
    months = '|'.join(calendar.month_name[1:])
    match = re.search(r'\b(' + months + r')\s+(20\d{2})\b', text, re.I)
    if not match:
        return None
    target = (int(match[2]), list(m.lower() for m in calendar.month_name).index(match[1].lower()))
    now = timezone.now().date()
    count = (target[0] - now.year) * 12 + target[1] - now.month
    if not 1 <= count <= 24:
        return None
    lines = ['Here is a provisional month-by-month plan for your ' + match[0] +
             ' start. These are planning milestones, not university deadlines; check each programme and scholarship deadline now and move tasks earlier where needed.']
    tasks = [
        'Choose your degree and countries, confirm your total budget, and start a shortlist. Record official programme and scholarship deadlines.',
        'Check prerequisites and accepted English tests. Arrange transcripts and ask potential recommenders. Book any required tests.',
        'Draft your statement and CV; gather project/internship evidence. Prepare applications whose deadlines come first.',
        'Submit applications as they become ready before their actual deadlines. Track missing documents, scholarships and replies.',
    ]
    for i in range(count + 1):
        index = now.year * 12 + now.month - 1 + i
        year, month0 = divmod(index, 12)
        remaining = count - i
        if i < min(3, count):
            task = tasks[i]
        elif remaining == 0:
            task = 'Complete enrolment, attend orientation and check arrival requirements.'
        elif remaining <= 2:
            task = 'Complete outstanding offer conditions and immigration steps where applicable. Confirm housing and travel only when your plans are secure.'
        elif remaining <= 4:
            task = 'Compare actual offers and full costs, check acceptance dates, and prepare required funding and visa documents.'
        else:
            task = tasks[3]
        lines.append('**' + calendar.month_name[month0 + 1] + ' ' + str(year) + ':** ' + task)
    return '\n\n'.join(lines)
