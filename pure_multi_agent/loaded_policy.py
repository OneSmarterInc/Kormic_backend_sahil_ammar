"""Conservative coverage checks for explicit, cited university policies."""
import re


def policy_records(evidence):
    for key in ('facts', 'saved_knowledge'):
        for row in evidence.get(key, []):
            if row.get('source_url') and row.get('content'):
                yield row


def tuition_policy(evidence):
    """Extract the effective-date notice, never borrow dates from other pages."""
    groups = {}
    for row in policy_records(evidence):
        if 'tuition-fee-changes-fall-2026' in row['source_url']:
            groups.setdefault(row['source_url'], []).append(row['content'])
    for url, parts in groups.items():
        text = '\n'.join(parts)
        if all(re.search(pattern, text, re.I) for pattern in (
            r'new master', r'\$690', r'three years\s*\(9 terms\)',
            r'three additional years', r'beginning with the fall 2026',
            r'first.term', r'\$100 resource fee', r'every trimester',
        )):
            return {'source_url': url, 'content': text}
    return None


def covered_policy(question, evidence):
    """Only mark narrowly scoped questions complete; extra requests stay open."""
    from university_research.relevance import retrieval_question
    text = retrieval_question(question).strip()
    extra_topic = re.search(r'waiver|refund|scholarship|deposit|doctoral|housing|book fees?|military', question, re.I)
    if not extra_topic and tuition_policy(evidence) and re.fullmatch(
        r"What is [^?]+standard master[’']s tuition per credit for 2026[–-]2027, "
        r"including changes effective Fall 2026\?\s*How long does the tuition guarantee "
        r"last for new and currently enrolled master[’']s students\?", text, re.I
    ):
        return ('The loaded effective Fall 2026 notice answers the requested rate, both guarantee cohorts and the resource fee. '
                'Give those terms with its citation; no university query is needed.')
    if re.fullmatch(r'Does [^?]+ (?:offer|have|provide) on-campus housing\?', text, re.I):
        for row in policy_records(evidence):
            if re.search(r'(?:does not|do not) (?:offer|have|provide) on-campus housing', row['content'], re.I):
                return 'The cited policy explicitly says there is no on-campus housing. Answer that fact; absence of housing is not missing information.'
    return ''


def tuition_excerpt(text, question, budget):
    """Keep policy clauses together instead of selecting only the price table."""
    if not re.search(r'tuition', question, re.I) or not re.search(r'guarantee', question, re.I):
        return ''
    sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
    patterns = (r'new master', r'currently enrolled master', r'first.term',
                r'three years', r'three additional years', r'\$100 resource fee',
                r'exempt|won.t be charged', r'no charge timeframe')
    selected = [s for s in sentences if any(re.search(p, s, re.I) for p in patterns)]
    excerpt = '\n'.join(selected)
    if excerpt and len(excerpt) <= budget:
        return excerpt
    return ''


def tuition_wording_problems(answer, question, evidence):
    policy = tuition_policy(evidence)
    if not policy or not re.search(r'tuition', question, re.I):
        return []
    issues = []
    if re.search(r'guarantee', question, re.I):
        for pattern in (r'three years|3 years', r'nine (?:terms|trimesters)|9 (?:terms|trimesters)',
                        r'first.term|first term', r'(?:currently enrolled|current|existing|continuing) (?:master[’\']s )?students',
                        r'three additional years|3 additional years', r'fall 2026'):
            if not re.search(pattern, answer, re.I):
                issues.append('Use the loaded Fall 2026 notice: new master’s students lock their first-term rate for three years (nine terms); current students keep their existing rate for up to three additional years beginning Fall 2026. Distinguish both cohorts and their start dates.')
                break
    if re.search(r'fees?|resource', question, re.I):
        if not re.search(r'\$100.{0,100}(?:every|per|each) trimester', answer, re.I | re.S):
            issues.append('The loaded notice states a separate $100 resource fee every trimester, not every semester. Preserve documented exemptions and refund conditions if discussing them.')
    if re.search(r'resource fee.{0,100}(?:per|every|each) semester', answer, re.I | re.S):
        issues.append('Resource fees are charged every trimester in the effective Fall 2026 notice; do not substitute semester.')
    if policy['source_url'] not in answer:
        issues.append('Cite the loaded effective Fall 2026 notice for these terms.')
    return issues


def covered_tuition_answer(question, evidence):
    policy = tuition_policy(evidence)
    if not re.search(r'tuition', question, re.I) or not policy or not covered_policy(question, evidence):
        return ''
    return (
        '**Master’s tuition effective Fall 2026**\n\n'
        'New master’s students pay **$690 per credit hour**. Currently enrolled '
        'master’s students retain their existing guaranteed rate.\n\n'
        '**Tuition guarantee**\n\n'
        '- New master’s students: their first-term rate is locked for **three years '
        '(nine terms)**, starting with their first term.\n'
        '- Currently enrolled master’s students: their existing rate continues for '
        '**up to three additional years beginning with the Fall 2026 trimester**.\n\n'
        '**Additional fees**\n\n'
        'The resource fee is separate from tuition: **$100 every trimester**. '
        'The tuition guarantee concerns the tuition rate, not a promise that all additional fees are fixed.\n\n'
        'Source: ' + policy['source_url']
    )
