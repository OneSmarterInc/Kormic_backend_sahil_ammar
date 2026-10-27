"""Deterministic checks for fabricated links and quantitative source drift."""
import re
import json


def applicant_gpa_comparison(question, evidence):
    """Compute range membership, never infer admission from a score alone."""
    from decimal import Decimal
    if not re.search(r'conditional', question, re.I):
        return None
    score = re.search(r'\b(\d(?:\.\d+)?)\s+GPA\b', question, re.I)
    if not score:
        return None
    ranges = set()
    for fact in evidence.get('facts', []):
        content = fact.get('content', '')
        if not re.search(r'conditional', content, re.I):
            continue
        for low, high in re.findall(
                r'\bGPA\s+(?:between\s+|of\s+)?(\d\.\d+)\s*(?:[–—-]|and|to)\s*(\d\.\d+)', content, re.I):
            if Decimal(low) <= Decimal(high):
                ranges.add((low, high))
    if len(ranges) != 1:
        return None  # Different programmes/ranges require qualified reasoning.
    low, high = ranges.pop()
    value = Decimal(score[1])
    relation = 'within' if Decimal(low) <= value <= Decimal(high) else 'outside'
    return {'score': score[1], 'low': low, 'high': high, 'relation': relation}


def general_conditional_policy(question, evidence):
    """Only resolve a general domestic master's range question, not a programme decision."""
    from university_research.relevance import retrieval_question
    text = retrieval_question(question)
    if not re.fullmatch(
            r"Can\s+a\s+domestic\s+master[’']?s\s+applicant\s+with\s+a\s+\d(?:\.\d+)?\s+GPA\s+"
            r"(?:receive|qualify\s+for)\s+conditional\s+admission(?:\s+at\s+[\w .'’–-]+)?\??", text, re.I):
        return None
    if not re.search(r'\bdomestic\b', text, re.I) or not re.search(r'master', text, re.I):
        return None
    if re.search(r'\b(?:nursing|MSN|MBA|international|exception|guarantee|appeal)\b|master.{0,5}\s+in\s+', text, re.I):
        return None
    comparison = applicant_gpa_comparison(question, evidence)
    if not comparison or comparison['relation'] != 'within':
        return None
    for fact in evidence.get('facts', []):
        content = fact.get('content', '')
        scope = str(fact.get('topic', '')) + ' ' + str(fact.get('source_url', '')) + ' ' + content
        if (re.search(r'domestic', content, re.I) and re.search(r'master', scope, re.I)
                and 'admission' in str(fact.get('source_url', '')).casefold()
                and 'conditional' in content.casefold() and fact.get('source_url')
                and comparison['low'] in content and comparison['high'] in content):
            return comparison
    return None


def violations(answer, evidence, profile=None, *, question=''):
    corpus = json.dumps(evidence, ensure_ascii=False, default=str)
    problems = []
    if re.search(r'\b(?:let me|I (?:will|shall)|I\'ll)\s+(?:now\s+)?(?:retrieve|search|check|look up|gather|fetch)\b', answer, re.I):
        problems.append('This is an unfinished retrieval promise. Give the supported answer now and explicitly mark remaining requested facts unknown; do not promise another search.')
    urls = re.findall(r'https?://[^\s<>\]\)"\\]+', answer)
    for url in urls:
        if url.rstrip('/.,') not in corpus:
            problems.append('An answer link was not supplied by the evidence.')
    # A plausible-sounding office referral is still an unsupported claim.
    # Match explicit unit names, not generic references to university staff.
    for office in re.findall(r'\bOffice of [A-Z][a-z]+(?: (?:[A-Z][a-z]+|and|of|the))*', answer):
        if office.casefold() not in corpus.casefold():
            problems.append('An office referral was not supplied by the evidence. Omit unsupported referrals and answer only the requested policy.')
    # Numeric dates, fees, durations and thresholds must occur in evidence or
    # the actual student profile. Ignore Markdown list numbering.
    text = re.sub(r'(?m)^\s*\d+[.)]\s+', '', answer)
    text = re.sub(r'https?://\S+', '', text)
    def numbers(value):
        from decimal import Decimal
        result = {str(Decimal(n.replace(',', '')).normalize()) for n in re.findall(r'(?<!\w)\d[\d,]*(?:\.\d+)?', value)}
        # Academic-year headings commonly abbreviate the second year. Treat
        # 2026-27 as evidence for 2027, without accepting unrelated years.
        for start, end in re.findall(r'\b((?:19|20)\d{2})\s*[–—/-]\s*(\d{2})(?!\d)', value):
            following = int(start) + 1
            if following % 100 == int(end):
                result.add(str(following))
        return result
    # Applicant numbers are evidence only for statements about the applicant,
    # never institution tuition, eligibility thresholds or scholarship awards.
    student_numbers = numbers(json.dumps(profile or {}, default=str))
    source_numbers = numbers(corpus)
    # A hypothetical applicant's score need not occur verbatim in a university
    # policy (which normally gives a range). It is input, never a new threshold.
    gpa_inputs = numbers(' '.join(re.findall(
        r'(?:\b\d(?:\.\d+)?\s+GPA\b|\bGPA\s*(?:is|of|:|=)?\s*\d(?:\.\d+)?)',
        question, re.I,
    )))
    unsupported = set()
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        applicant_statement = re.search(r'\b(?:your|you have|you scored)\b', sentence, re.I)
        institution_claim = re.search(r'\b(?:tuition|minimum|required|threshold|scholarship|deadline|seats?|admission requirement)\b', sentence, re.I)
        allowed = source_numbers | (student_numbers if applicant_statement and not institution_claim else set())
        # Exempt only the attributed input occurrence, not every instance of the
        # number in this sentence. A subsequent invented threshold still fails.
        plain_sentence = sentence.replace('**', '')
        def applicant_input(match):
            return 'applicant GPA' if numbers(match.group()) <= gpa_inputs else match.group()
        checked = re.sub(
            r'\b(?:with\s+(?:an?\s+)?|your\s+)\d(?:\.\d+)?\s+GPA\b|'
            r"\b(?:your|the applicant['’]s|this applicant['’]s)\s+GPA\s+(?:is\s+|of\s+)?\d(?:\.\d+)?\b",
            applicant_input, plain_sentence, flags=re.I,
        )
        unsupported.update(numbers(checked) - allowed)
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
    # A GRE mention must not borrow a quantifier from unrelated requirements
    # (for example, transcripts from all schools attended in another paragraph).
    for sentence in re.split(r'(?<=[.!?])\s+|\n', answer):
        gre_claim = re.sub(r'\bnot\s+(?:virtually\s+)?all\b', 'some', sentence, flags=re.I)
        if re.search(r'\bGRE\b', gre_claim, re.I) and re.search(r'\b(all|virtually all|non-negotiable|cannot apply|compulsory everywhere)\b', gre_claim, re.I):
            problems.append('Do not generalize GRE requirements across institutions; verify the exact programme policy.')
            break
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
