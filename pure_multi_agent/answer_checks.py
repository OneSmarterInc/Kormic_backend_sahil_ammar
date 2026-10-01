"""Deterministic checks for fabricated links and quantitative source drift."""
import re
import json


def violations(answer, evidence, profile=None):
    corpus = json.dumps(evidence, ensure_ascii=False, default=str)
    problems = []
    if re.search(r'\b(?:let me|I (?:will|shall)|I\'ll)\s+(?:now\s+)?(?:retrieve|search|check|look up|gather|fetch)\b', answer, re.I):
        problems.append('This is an unfinished retrieval promise. Give the supported answer now and explicitly mark remaining requested facts unknown; do not promise another search.')
    urls = re.findall(r'https?://[^\s<>\]\)"\\]+', answer)
    for url in urls:
        if url.rstrip('/.,') not in corpus:
            problems.append('An answer link was not supplied by the evidence.')
    # Numeric dates, fees, durations and thresholds must occur in evidence or
    # the actual student profile. Ignore Markdown list numbering.
    text = re.sub(r'(?m)^\s*\d+[.)]\s+', '', answer)
    text = re.sub(r'https?://\S+', '', text)
    def numbers(value):
        from decimal import Decimal
        return {str(Decimal(n.replace(',', '')).normalize()) for n in re.findall(r'(?<!\w)\d[\d,]*(?:\.\d+)?', value)}
    # Applicant numbers are evidence only for statements about the applicant,
    # never institution tuition, eligibility thresholds or scholarship awards.
    student_numbers = numbers(json.dumps(profile or {}, default=str))
    source_numbers = numbers(corpus)
    unsupported = set()
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        applicant_statement = re.search(r'\b(?:your|you have|you scored)\b', sentence, re.I)
        institution_claim = re.search(r'\b(?:tuition|minimum|required|threshold|scholarship|deadline|seats?|admission requirement)\b', sentence, re.I)
        allowed = source_numbers | (student_numbers if applicant_statement and not institution_claim else set())
        unsupported.update(numbers(sentence) - allowed)
    if unsupported:
        problems.append('Unsupported numeric claims: ' + ', '.join(sorted(unsupported)))
    tuition_numbers, contribution_numbers = set(), set()
    def fee_fields(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r'tuition', str(key), re.I):
                    tuition_numbers.update(numbers(str(item)))
                elif re.search(r'contribution|semester_fee|administrative_fee', str(key), re.I):
                    contribution_numbers.update(numbers(str(item)))
                fee_fields(item)
        elif isinstance(value, list):
            for item in value:
                fee_fields(item)
    fee_fields(evidence)
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        if re.search(r'\btuition\b', sentence, re.I) and not re.search(r'\bcontribution\b|administrative', sentence, re.I):
            if numbers(sentence) & (contribution_numbers - tuition_numbers):
                problems.append('A semester or administrative contribution is not tuition. Keep these amounts separate.')
    for test in ('IELTS', 'TOEFL', 'GRE'):
        if test.lower() not in corpus.lower() and re.search(r'\b' + test + r'\b', answer, re.I):
            for paragraph in answer.split('\n\n'):
                if re.search(r'\b' + test + r'\b', paragraph, re.I) and re.search(r'\b(acceptable|sufficient|meets?|required|minimum|waived)\b', paragraph, re.I) and not re.search(r'\b(unknown|unverified|not.*(?:found|available|confirm)|cannot|can.t)\b', paragraph, re.I):
                    problems.append(test + ' admission conclusion has no matching official test evidence; describe it as unverified, not acceptable or required.')
                    break
    if re.search(r'\bGRE\b', answer, re.I) and re.search(r'\b(all|virtually all|non-negotiable|cannot apply|compulsory everywhere)\b', answer, re.I):
        problems.append('Do not generalize GRE requirements across institutions; verify the exact programme policy.')
    return problems


def supported_sections(answer, evidence, profile=None):
    """A bad optional detail must not discard the supported remainder."""
    kept = [line for line in answer.splitlines() if not violations(line, evidence, profile)]
    while kept and (not kept[-1].strip() or kept[-1].lstrip().startswith('#')):
        kept.pop()
    text = '\n'.join(kept).strip()
    return text + '\n\nSome requested details could not be verified from the available sources.'


def clean_tool_names(answer, tool_names):
    for name in tool_names:
        answer = answer.replace('`' + name + '`', 'the relevant Kormic feature')
        answer = re.sub(r'\b' + re.escape(name) + r'\b', 'the relevant Kormic feature', answer)
    return answer
