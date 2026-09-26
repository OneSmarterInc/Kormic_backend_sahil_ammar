"""Persist observed conversation and resolved institutions; no keyword routing."""
from datetime import datetime, timezone


def update_memory(ctx, user_message, reply):
    memory = ctx['memory']
    if user_message.strip():
        points = memory.setdefault('important_points', [])
        points.append({'time': datetime.now(timezone.utc).isoformat(), 'user': user_message[:3000], 'aria': (reply or '')[:500]})
        memory['important_points'] = points[-50:]
    names = memory.setdefault('universities_discussed', [])
    for ref in ctx.get('university_references', {}).values():
        if ref['name'] not in names:
            names.append(ref['name'])
    memory['universities_discussed'] = names[-100:]
