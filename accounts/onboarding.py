from django.db import transaction
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from accounts.models import Account
from accounts.permissions import IsStudentRole, IsTOTPEnrolled
from accounts.serializers import student_onboarding_status

class PreferenceSerializer(serializers.Serializer):
    github_onboarding_state = serializers.ChoiceField(choices=["skipped", "required"], required=False)
    linkedin_onboarding_state = serializers.ChoiceField(choices=["skipped", "required"], required=False)

    def validate(self, data):
        if not data or set(self.initial_data) - set(self.fields):
            raise serializers.ValidationError("Supply only GitHub/LinkedIn onboarding preferences.")
        return data

class OnboardingPreferencesView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]

    def patch(self, request):
        serializer = PreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            account = Account.objects.select_for_update().get(user=request.user)
            preferences = dict(account.onboarding_preferences or {})
            preferences.update(serializer.validated_data)
            account.onboarding_preferences = preferences
            account.save(update_fields=["onboarding_preferences", "updated_at"])
        return Response({"onboarding": student_onboarding_status(account.student_uuid)})
