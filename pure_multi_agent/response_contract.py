"""Student-visible output protocol. Does not rewrite valid prose."""
import re


def problems(text, *, saved=False):
    issues = []
    if re.search(r'\b(?:let me|I (?:will|need to|should))\s+(?:check with|ask)\s+the student\b|\bask the student if this is correct\b', text, re.I):
        issues.append('Address the student directly. Answer the requested review from saved evidence; do not repeat internal verification instructions or promise to ask the student.')
    if re.search(r'</?tool>|"(?:arguments|tool_calls)"\s*:|\b(?:review_student_profile|calculate_study_budget|search_study_resources|save_advising_artifact|university_reply_status|resolve_profile_change|update_student_profile)\b', text):
        issues.append('Return a student-facing answer only, not tool syntax, function names or instructions to invoke tools.')
    if re.search(r'validation error|unsupported numeric claim|tool call was rejected|correct (?:the|your) (?:arguments|tool)|allowed tools:', text, re.I):
        issues.append('Do not expose internal validation or repair instructions. Answer the current student question.')
    if re.search(r'\{(?:fee|seats|tuition|deadline)\}|<\|[^>]+\|>', text, re.I):
        issues.append('Do not display template placeholders as facts; explain a missing requested detail naturally.')
    if not saved and re.search(r'\b(?:I (?:have )?(?:saved|updated|changed)|your (?:profile|preferences|budget|destination) (?:has|have) been (?:saved|updated|changed))\b', text, re.I):
        issues.append('No successful profile-write receipt exists for this turn. Do not claim anything was saved or changed.')
    return issues


def admission_claims(text, evidence):
    """General advice must not turn an applicant score into an entry threshold."""
    from .answer_checks import violations
    issues = []
    for paragraph in text.split('\n\n'):
        if re.search(r'\b(?:minimum|at least|threshold|required score|requires? a)\b', paragraph, re.I) and re.search(r'\d', paragraph):
            issues.extend(violations(paragraph, evidence))
        if re.search(r'\b(?:cost|tuition|fees)\b.{0,50}\b(?:is|are|estimated|amounts? to)\b', paragraph, re.I) and re.search(r'\d', paragraph):
            issues.extend(violations(paragraph, evidence))
        if not evidence and re.search(r'\b(?:you are eligible|your .{0,100} is eligible)\b', paragraph, re.I):
            issues.append('Eligibility has not been established for a specific programme. Explain that admission depends on the institution, degree recognition and prerequisites.')
    return issues
