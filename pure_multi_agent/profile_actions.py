"""Explicit preference edits use transactional receipts, never generated success claims."""
import re
from decimal import Decimal


def money_amount(text):
    match = re.search(r'(?<!\w)(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|crore|million|thousand)?\b', text, re.I)
    if not match:
        return None
    factor = {'lakh':100000, 'lac':100000, 'crore':10000000, 'million':1000000, 'thousand':1000}
    return float(Decimal(match[1].replace(',', '')) * factor.get((match[2] or '').lower(), 1))


def describe(values):
    labels = {'budget':'Budget', 'budget_text':'Budget details', 'preferences.preferred_locations':'Preferred destination',
              'preferences.preferred_intake':'Preferred intake'}
    lines = []
    for key, value in values.items():
        if key == 'budget_text':
            continue
        if key == 'budget':
            unit = re.search(r'\b(INR|USD|GBP|EUR|CAD|AUD)\b', str(values.get('budget_text', '')), re.I)
            value = (unit[0].upper() + ' ' if unit else '') + f'{value:,.0f}'
        elif isinstance(value, list):
            value = ', '.join(map(str, value))
        lines.append(f'- **{labels.get(key, key.replace("_", " ").capitalize())}:** {value}')
    return '\n'.join(lines)


def receipt(result):
    if result.get('error'):
        return 'I could not save this change. Your existing profile is unchanged. Please clarify the value you want to update.'
    if 'saved_missing_fields' in result:
        saved, pending = result['saved_missing_fields'], result.get('confirmation_required')
        parts = ['Saved to your profile:\n' + describe(saved)] if saved else []
        if pending:
            parts.append('Please confirm replacing these saved preferences:\n' + describe(pending['after']) + '\n\nThese replacements have not been saved yet.')
        if not parts:
            parts = ['These values are already saved in your profile.']
        return '\n\n'.join(parts)
    if result.get('status') == 'applied':
        return 'Saved to your profile:\n' + describe(result['after'])
    if result.get('status') in ('rejected', 'cancelled'):
        return 'Your saved profile is unchanged.' + (' I will use the proposed values only as temporary assumptions for this conversation.' if result.get('assumption_active') else ' The proposed change has been discarded.')
    return 'This change was not saved. Please confirm the current values before trying again.'


def explicit_values(text):
    values = {}
    destination = re.search(r'\b(?:preferred\s+)?destination\s+(?:is|to|:)\s*([A-Za-z][A-Za-z ]{1,55}?)(?=\s+and\b|[.,;!?]|$)', text, re.I)
    if destination:
        values['preferences.preferred_locations'] = [destination[1].strip()]
    budget = re.search(r'(?:\b(?:total\s+)?budget\s*(?:is|to|:)\s*)?((?:INR|USD|GBP|EUR|CAD|AUD)\s*\d[\d,.]*(?:\s*(?:lakh|lac|crore|million|thousand))?|\d[\d,.]*(?:\s*(?:lakh|lac|crore|million|thousand))?\s*(?:INR|USD|GBP|EUR|CAD|AUD))', text, re.I)
    if budget and re.search(r'\bbudget\b', text, re.I) and not re.search(r'keep (?:my |the )?budget unchanged', text, re.I):
        values['budget'] = money_amount(budget[1])
        values['budget_text'] = budget[1] + (' total' if re.search(r'\btotal\b', text, re.I) else '')
    return values


def handle(ctx):
    text = ctx.get('current_message', '').strip()
    if not ctx.get('canonical_student_id') or ctx.get('chat_attachments') or ctx.get('documents_read'):
        return None
    if re.search(r'\b(if|suppose|hypothetical|example|friend|brother|sister)\b|["“”]', text, re.I):
        return None
    from .change_proposals import conversation_state, update_student, resolve, same
    if re.search(r'\b(?:what|which)\b.*\b(?:saved|stored)\b', text, re.I) and re.search(r'\b(budget|country|destination|preferences)\b', text, re.I):
        from django_api.models import StudentProfile
        from django_api.services import profile_row_to_dict
        profile = profile_row_to_dict(StudentProfile.objects.get(uuid=ctx['canonical_student_id']))
        values = {'preferences.preferred_locations':(profile.get('preferences') or {}).get('preferred_locations') or ['Not set'],
                  'budget':profile.get('budget'), 'budget_text':profile.get('budget_text', '')}
        return 'Your saved preferences are:\n' + describe({k:v for k,v in values.items() if v is not None})
    decision = 'approve' if re.match(r'^(?:yes\b|confirm\b|approve\b)', text, re.I) else ('cancel' if re.match(r'^cancel\b', text, re.I) else None)
    if decision:
        pending = [p for p in conversation_state(ctx)['pending_changes'] if p['kind'] == 'student_profile']
        if len(pending) != 1:
            return 'There is no single pending profile change to confirm. Please tell me the exact preference you want to save.' if pending or re.search(r'changes|preferences|budget|destination', text, re.I) else None
        proposal = pending[0]
        if decision == 'approve':
            prefix, separator, details = text.partition(':')
            consent = re.fullmatch(r'(?:yes[,.! ]*)?(?:(?:please )?(?:confirm|approve|save)(?: (?:it|them|those changes|these changes|the changes|those preferences|the pending changes))?)?[.! ]*', prefix, re.I)
            remainder = details
            if separator:
                locations = proposal['after'].get('preferences.preferred_locations',
                    (ctx.get('student_profile', {}).get('preferences') or {}).get('preferred_locations', []))
                for location in locations:
                    remainder = re.sub(r'\b' + re.escape(location) + r'\b', '', remainder, flags=re.I)
                amount = money_amount(details)
                if amount is not None and same(amount, proposal['after'].get('budget')):
                    remainder = re.sub(r'\b\d[\d,.]*(?:\s*(?:lakh|lac|crore|million|thousand))?\b', '', remainder, flags=re.I)
                    currency = re.search(r'\b(INR|USD|GBP|EUR|CAD|AUD)\b', proposal['after'].get('budget_text', ''), re.I)
                    if currency:
                        remainder = re.sub(r'\b' + currency[0] + r'\b', '', remainder, flags=re.I)
                remainder = re.sub(r'\b(and|total|budget|destination|preferred)\b|[\s.,;]', '', remainder, flags=re.I)
            if not consent or remainder.strip():
                return 'Please confirm the pending values with “confirm those changes”, or state the revised preferences you want to save.'
        values = explicit_values(text)
        if any(k in proposal['after'] and k != 'budget_text' and not same(v, proposal['after'][k]) for k,v in values.items()):
            return 'Those values differ from the pending change. Please state the replacement values you want to save.'
        # Do not interpret conditional/revised or quoted permission as consent.
        if re.search(r'\b(but|instead|unless|change|except)\b', text, re.I):
            return 'Please confirm the pending values without changes, or tell me the revised preferences.'
        result = resolve(ctx, proposal['id'], decision, text)
    elif re.search(r'\b(update|change|save|set)\b', text, re.I):
        values = explicit_values(text)
        if not values:
            return None
        result = update_student(ctx, values)
    else:
        return None
    ctx['profile_write_receipt'] = result
    return receipt(result)
