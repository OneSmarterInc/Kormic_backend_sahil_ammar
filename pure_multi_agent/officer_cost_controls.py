"""Choose an obvious initial read, never create or approve an officer edit."""
import re


def initial_read(message):
    text = message.strip()
    if not text or len(text) > 6000 or '\n' in text:
        return None
    # Only fetch status. The model and the existing consent validator still decide
    # whether a later-turn reply can resolve a specific displayed proposal.
    if re.fullmatch(r'(?:yes|no|approve|reject|cancel|confirm)(?: please)?[.!]?', text, re.I):
        return {'name': 'university_change_status', 'args': {}}
    if not re.match(r'^(?:(?:please|can you|could you)\s+)*(?:show|list|read|review|what|which|tell|update|change|add|edit|set)\b', text, re.I):
        return None
    # Context-dependent, mixed-action and student-specific turns retain normal reasoning.
    if re.search(r'\b(student|students|candidate|candidates|applicant profile|send|delete|approve|reject|confirm|cancel|previous|earlier|above|same|instead|except)\b', text, re.I):
        return None
    categories = {
        'requirements': r'\b(cgpa|gpa|admission requirements?|entry requirements?|eligibility criteria)\b',
        'scholarship': r'\bscholarships?\b',
        'fees': r'\b(tuition|fees?)\b',
        'contacts': r'\b(contact email|contact phone|admissions office address)\b',
        'profile': r'\b(university name|university description|university website|tagline)\b',
        'agent': r'\b(agent name|agent settings|communication style)\b',
        'sources': r'\b(knowledge sources|scraping status|crawl status)\b',
    }
    matches = [category for category, pattern in categories.items() if re.search(pattern, text, re.I)]
    if len(matches) != 1:
        return None
    category = matches[0]
    edit = bool(re.match(r'^(?:(?:please|can you|could you)\s+)*(?:update|change|add|edit|set)\b', text, re.I))
    if category in ('scholarship', 'fees'):
        # Search the complete question: do not infer a target record ID or strip conditions.
        plan = {'name': 'search_university_knowledge', 'args': {'question': text}}
        proposal = 'propose_knowledge_change'
    else:
        plan = {'name': 'read_university_record', 'args': {'section': category}}
        proposal = ('propose_admission_requirement' if category == 'requirements'
                    else 'propose_university_information')
    if edit and category != 'sources':
        plan['proposal_tool'] = proposal
    return plan
