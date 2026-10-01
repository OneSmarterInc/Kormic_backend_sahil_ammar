"""Deliver university-agent answers without rewriting them as evidence templates."""


def grounded_reply(ctx, draft):
    """Deliver a completed university consultation without a second formatter."""
    evidence = ctx.get('university_answer_evidence')
    if isinstance(evidence, dict) and evidence.get('agent_answer') and evidence.get('answer_turn') == ctx.get('turn_id'):
        return evidence['agent_answer']
    return draft
