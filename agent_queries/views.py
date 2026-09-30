from django.db.models import Q, Case, When, Value, IntegerField, Count
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from accounts.permissions import IsTOTPEnrolled, IsSuperUserRole, get_account
from .models import AgentQuery, AgentConversation
from .services import answer_query, names


def pagination(request, rows, size=10):
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (ValueError, TypeError):
        raise ValidationError("Invalid page.")
    total = rows.count()
    return rows[(page-1)*size:page*size], {"page": page, "page_size": size, "total": total, "has_next": page*size < total}


def scoped_queries(account):
    rows = AgentQuery.objects.select_related("conversation__university", "conversation__student")
    if account and account.role == "student" and account.student_profile_id:
        return rows.filter(conversation__student_id=account.student_profile_id)
    if account and account.role == "university" and account.university_id:
        return rows.filter(conversation__university_id=account.university_id)
    raise PermissionDenied("Student or university account required.")


def serialize(row):
    return {"id": row.pk, "direction": row.direction, "question": row.question, "answer": row.answer,
        "status": row.status, "raised_by_agent": row.raised_by_agent, "recipient_agent": row.recipient_agent,
        "university_name": row.conversation.university.name, "student_name": row.conversation.student.name,
        "university_id": str(row.conversation.university.uuid), "student_id": str(row.conversation.student.uuid),
        "answer_scope": row.answer_scope, "created_at": row.created_at, "answered_at": row.answered_at}


class QueryListView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled]
    def get(self, request):
        rows = scoped_queries(get_account(request))
        selected = request.query_params.get("query_id")
        if selected:
            try: rows = rows.filter(pk=int(selected))
            except (ValueError, TypeError): raise ValidationError("Invalid query ID.")
        direction, status = request.query_params.get("direction"), request.query_params.get("status")
        if direction:
            if direction not in AgentQuery.Direction.values: raise ValidationError("Invalid direction.")
            rows = rows.filter(direction=direction)
        if status:
            if status not in ("answered", "unanswered"): raise ValidationError("Invalid status.")
            rows = rows.filter(status=status)
        query = request.query_params.get("search", "").strip()[:200]
        if query:
            rows = rows.filter(Q(question__icontains=query) | Q(answer__icontains=query) | Q(conversation__university__name__icontains=query) | Q(conversation__student__name__icontains=query) | Q(raised_by_agent__icontains=query))
        rows = rows.annotate(pending_first=Case(When(status="unanswered", then=Value(0)), default=Value(1), output_field=IntegerField())).order_by("pending_first", "-created_at", "-id")
        rows, page = pagination(request, rows)
        return Response({"results": [serialize(row) for row in rows], "pagination": page})


class AnswerInput(serializers.Serializer):
    answer = serializers.CharField(max_length=12000, allow_blank=False, trim_whitespace=True)
    answer_scope = serializers.ChoiceField(choices=["private", "university"], default="private")
    confirmed = serializers.BooleanField()


class QueryAnswerView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled]
    def post(self, request, query_id):
        account = get_account(request)
        if not scoped_queries(account).filter(pk=query_id).exists(): raise NotFound()
        data = AnswerInput(data=request.data); data.is_valid(raise_exception=True)
        if not data.validated_data['confirmed']: raise ValidationError("Confirm the answer before sending.")
        row = answer_query(query_id, account, data.validated_data['answer'], data.validated_data['answer_scope'])
        return Response(serialize(row))


class ConversationListView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsSuperUserRole]
    def get(self, request):
        rows = AgentConversation.objects.select_related("student", "university").annotate(message_count=Count("messages"))
        query = request.query_params.get("search", "").strip()[:200]
        if query:
            rows = rows.filter(Q(student__name__icontains=query) | Q(university__name__icontains=query) | Q(student__agent_name__icontains=query) | Q(university__agent_name__icontains=query))
        rows, page = pagination(request, rows.order_by("-updated_at", "id"))
        return Response({"results": [{"id": str(r.pk), "student_name": r.student.name, "university_name": r.university.name,
            "student_agent": names(r)["student_agent"], "university_agent": names(r)["university_agent"],
            "message_count": r.message_count, "updated_at": r.updated_at, "created_at": r.created_at} for r in rows], "pagination": page})


class ConversationDetailView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsSuperUserRole]
    def get(self, request, conversation_id):
        conv = AgentConversation.objects.select_related("student", "university").filter(pk=conversation_id).first()
        if not conv: raise NotFound()
        def serialized(row):
            return {'id': row.pk, 'actor': row.actor, 'actor_name': row.actor_name, 'kind': row.kind,
                'content': row.content, 'created_at': row.created_at, 'query_id': row.query_id, 'metadata': row.metadata}
        selected = request.query_params.get('message_id')
        if selected:
            if not selected.isdigit(): raise ValidationError('Invalid message ID.')
            message = conv.messages.filter(pk=selected).first()
            if not message: raise NotFound()
            exchange = (message.metadata or {}).get('exchange_id')
            if exchange:
                steps = conv.messages.filter(metadata__exchange_id=exchange).order_by('id')
            else:
                start = conv.messages.filter(kind='request', id__lte=message.pk).order_by('-id').first()
                steps = conv.messages.filter(id__gte=start.pk if start else message.pk)
                next_request = conv.messages.filter(kind='request', id__gt=start.pk if start else message.pk).order_by('id').first()
                if next_request: steps = steps.filter(id__lt=next_request.pk)
                steps = steps.order_by('id')
            from django_api.models import AgentAuditLog
            logs = AgentAuditLog.objects.none()
            if exchange:
                logs = AgentAuditLog.objects.filter(student_id=str(conv.student.uuid), inputs__exchange_id=exchange).order_by('id')
            return Response({'message_id': message.pk, 'steps': [serialized(row) for row in steps],
                'logs': list(logs.values('id', 'student_id', 'run_id', 'actor_agent', 'target', 'action_type', 'inputs', 'outputs', 'timestamp')),
                'trace_available': bool(exchange)})
        if any(key in request.query_params for key in ('latest', 'before_id', 'after_id')):
            before, after = request.query_params.get('before_id'), request.query_params.get('after_id')
            if before and after: raise ValidationError('Use one message cursor.')
            if any(value and not value.isdigit() for value in (before, after)): raise ValidationError('Invalid message cursor.')
            messages = conv.messages.all()
            if before: messages = messages.filter(id__lt=int(before))
            if after: messages = messages.filter(id__gt=int(after))
            rows = list(messages.order_by('id' if after else '-id')[:31])
            has_more = len(rows) > 30
            rows = rows[:30]
            if not after: rows.reverse()
            return Response({'id': str(conv.pk), 'student_name': conv.student.name, 'student_id': str(conv.student.uuid),
                'university_name': conv.university.name, 'student_agent': names(conv)['student_agent'], 'university_agent': names(conv)['university_agent'],
                'results': [serialized(row) for row in rows], 'has_older': has_more if not after else False,
                'has_more': has_more, 'pagination': {'page':1, 'total':conv.messages.count(), 'has_next':False}})
        rows, page = pagination(request, conv.messages.order_by("created_at", "id"), 30)
        return Response({"id": str(conv.pk), "student_name": conv.student.name, "university_name": conv.university.name,
            "results": [{"id": r.pk, "actor": r.actor, "actor_name": r.actor_name, "kind": r.kind,
                "content": r.content, "created_at": r.created_at, "query_id": r.query_id, "metadata": r.metadata} for r in rows], "pagination": page})
