"""Rank the complete catalogue before applying evidence limits."""
import re
from django.db.models import Case, When, Value, IntegerField

STOP_WORDS = {'what', 'which', 'are', 'the', 'for', 'and', 'with', 'from', 'about',
              'please', 'tell', 'university', 'college', 'this', 'that', 'does', 'have',
              'can', 'you', 'me', 'of', 'in', 'to', 'is', 'at', 'my'}

def question_terms(question):
    terms = list(dict.fromkeys(t for t in re.findall(r'\w{2,}', question.casefold()) if t not in STOP_WORDS))[:20]
    if re.search(r'admission|requirement|eligib', question, re.I):
        terms += ['admission', 'requirement', 'eligib', 'prerequisite']
    return list(dict.fromkeys(terms))

def rank_records(rows, question, fields):
    score = Value(0, output_field=IntegerField())
    for field, weight in fields:
        for term in question_terms(question):
            score = score + Case(When(**{field + '__icontains': term}, then=Value(weight)),
                                 default=Value(0), output_field=IntegerField())
    return rows.annotate(question_relevance=score).order_by('-question_relevance', '-fetched_at', 'pk')
