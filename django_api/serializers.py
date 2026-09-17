from rest_framework import serializers

# Numeric fields left blank by a form submit as "" rather than omitted/null.
# IntegerField/FloatField's allow_null only tolerates JSON null, not "", so
# without this they 400 on every empty numeric field.
NULLABLE_NUMERIC_FIELDS = {
    "graduation_year", "gpa", "gre_quant", "gre_verbal", "toefl", "ielts", "budget", "work_months",
}


class ProfileCreateUpdateSerializer(serializers.Serializer):
    student_id = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    country = serializers.CharField(required=False, allow_blank=True)

    institution = serializers.CharField(required=False, allow_blank=True)
    major = serializers.CharField(required=False, allow_blank=True)
    program = serializers.CharField(required=False, allow_blank=True)
    graduation_year = serializers.IntegerField(required=False, allow_null=True)

    gpa = serializers.FloatField(required=False, allow_null=True)
    gpa_scale = serializers.CharField(required=False, allow_blank=True)

    gre_quant = serializers.IntegerField(required=False, allow_null=True)
    gre_verbal = serializers.IntegerField(required=False, allow_null=True)
    toefl = serializers.IntegerField(required=False, allow_null=True)
    ielts = serializers.FloatField(required=False, allow_null=True)
    english_score_text = serializers.CharField(required=False, allow_blank=True)

    budget = serializers.IntegerField(required=False, allow_null=True)
    work_months = serializers.IntegerField(required=False, allow_null=True)
    work_experience_summary = serializers.CharField(required=False, allow_blank=True)
    target_country = serializers.CharField(required=False, allow_blank=True)
    target_degree = serializers.CharField(required=False, allow_blank=True)
    preferred_specialization = serializers.CharField(required=False, allow_blank=True)

    github = serializers.CharField(required=False, allow_blank=True)
    linkedin_url = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        for field in NULLABLE_NUMERIC_FIELDS:
            if data.get(field, None) == "":
                data[field] = None

        if not data.get("english_score_text") and data.get("english_score") not in (None, ""):
            data["english_score_text"] = str(data["english_score"])

        return super().to_internal_value(data)


class ResumeUploadSerializer(serializers.Serializer):
    student_id = serializers.CharField(required=True)
    file = serializers.FileField(required=True)
