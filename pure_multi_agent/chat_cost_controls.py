"""Conservative chat savings: preserve evidence and fall back for ambiguous intent."""
import json
import re


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))


def simple_general_intent(text):
    """Only complete concept questions; personalized/compound requests use routing."""
    topic = r'(?:statement of purpose|sop|curriculum vitae|cv|resume|résumé|semester|credit hour|undergraduate degree|graduate degree|cover letter|letter of recommendation|transcript|academic transcript|gpa|cgpa|research proposal|study plan|application checklist|conditional offer|unconditional offer|tuition fee|application fee|assistantship|fellowship|scholarship)'
    if not re.fullmatch(r'\s*(?:what is (?:a |an |the )?|(?:please )?explain (?:a |an |the )?)' +
                       topic + r'(?: in (?:simple terms|detail))?\s*[?.!]*\s*', text, re.I):
        return None
    return {'route': 'general', 'institutions': [], 'comparison': False, 'followup': False,
            'country': '', 'requested_count': 0, 'reference_topic': 'none'}


def standalone_profile_intent(text):
    """Recognize a whole saved-profile advice request, never a write or follow-up."""
    prefix = r'\s*(?:please\s+)?(?:(?:can|could) you\s+)?'
    patterns = (
        r'(?:review|assess|evaluate|analy[sz]e) my (?:saved )?(?:student |academic )?profile',
        r'(?:how can i improve|suggest improvements (?:to|for)) my (?:student |academic )?profile',
        r'(?:what are|identify) (?:the )?(?:strengths and gaps|strengths and weaknesses) in my (?:saved )?profile',
    )
    if not any(re.fullmatch(prefix + pattern + r'(?:\s+in detail)?\s*[?.!]*\s*', text, re.I)
               for pattern in patterns):
        return None
    return {'route': 'profile', 'institutions': [], 'comparison': False, 'followup': False,
            'country': '', 'requested_count': 0, 'reference_topic': 'none'}


def standalone_document_review(text):
    return bool(re.fullmatch(
        r'\s*(?:please\s+)?(?:(?:can|could) you\s+)?(?:summarize|summarise|review|analyze|analyse|explain)\s+'
        r'(?:this|the|my)\s+(?:(?:uploaded|attached)\s+)?(?:pdf|document|file|resume|résumé|cv)'
        r'(?:\s+(?:briefly|in (?:detail|bullet points|simple terms)))?\s*[?.!]*\s*', text, re.I))


def unique_records(values):
    """Only identical records are redundant; differing dates/values/sources survive."""
    seen, result = set(), []
    for value in values:
        identity = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def explicit_comparison(text):
    """Recognize only a complete 'compare X and Y [for criteria]' request.

    No aliases or institution identities are inferred. The existing directory and
    official-site resolution still validate each literal name before retrieval.
    """
    match = re.fullmatch(r'\s*(?:please\s+)?compare\s+(.+?)\s*[?.!]*\s*', text, re.I)
    if not match or '\n' in text or len(text) > 6000:
        return None
    body = match[1]
    # Mixed write/document requests must retain normal classification and tools.
    if re.search(r'\b(update|change|save|set|confirm|approve|reject|cancel|delete|send|upload|attach\w*|document|resume|résumé|github|ignore|instead|except|not)\b', body, re.I):
        return None
    parts = re.split(r'\s+(?:for|on|regarding|in terms of)\s+|:\s*', body, maxsplit=1, flags=re.I)
    names = re.split(r'\s+(?:and|versus|vs\.?)\s+|,\s*(?:and\s+)?', parts[0], flags=re.I)
    if not 2 <= len(names) <= 5:
        return None
    clean = []
    for name in names:
        name = name.strip()
        if (not re.fullmatch(r"[\w .’'()\-]+", name)
                or not 2 <= len(name.split()) <= 12
                or len(re.findall(r'\b(university|college|institute)\b', name, re.I)) != 1
                or re.search(r'\b(fees?|costs?|scholarships?|deadlines?|tuition|eligibility|requirements?|programmes?|programs?|then|also|with)\b', name, re.I)):
            return None
        clean.append(name)
    if len({name.casefold() for name in clean}) != len(clean):
        return None
    # Do not turn a third institution embedded in the criteria into a dropped request.
    if len(parts) > 1 and re.search(r'\b(university|college|institute|compare|versus|vs)\b', parts[1], re.I):
        return None
    return {'route': 'university', 'institutions': clean, 'comparison': True,
            'followup': False, 'country': '', 'requested_count': 0, 'reference_topic': 'none'}
