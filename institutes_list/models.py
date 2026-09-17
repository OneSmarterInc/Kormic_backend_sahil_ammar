"""
Institute-list enrollment: institutes send student lists (for students headed
into university admissions); we pre-create unclaimed profiles; students claim
by proving control of the listed email.
Note: the uploading "institute" (institutes.Institute) is a distinct entity
from a "university" (universities.University) elsewhere in this codebase --
an institute is a local org that sends a list; a university is the AI-agent-
bearing destination school the student is applying to. Never conflate them.
"""
import secrets

from django.db import models


class UniversityStudentList(models.Model):
    """One uploaded list. The provenance record the whole tier stands on."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        WITHDRAWN = "withdrawn", "Withdrawn"

    institute = models.ForeignKey(
        "institutes.Institute", on_delete=models.PROTECT, related_name="student_lists"
    )
    contact_name = models.CharField(max_length=255)
    contact_email = models.CharField(max_length=255)
    # How the contact was verified (institutional domain, signed sponsor
    # authorization, ...). Free text on purpose: it is the human-readable
    # provenance note auditors read back.
    contact_verification = models.CharField(max_length=500, blank=True, default="")
    format_version = models.CharField(max_length=20, default="v1")
    uploaded_by = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    row_count = models.IntegerField(default=0)

    # The original uploaded sheet, kept verbatim as part of the provenance
    # record so an institute/superuser can download exactly what was sent.
    # Blank on lists uploaded before this was added.
    source_file_path = models.CharField(max_length=1000, blank=True, default="")
    source_file_name = models.CharField(max_length=500, blank=True, default="")
    source_file_content_type = models.CharField(max_length=150, blank=True, default="")
    source_file_size = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"List({self.institute_id}, rows={self.row_count}, {self.status})"


def _new_claim_token() -> str:
    return secrets.token_urlsafe(24)


class ListedStudent(models.Model):
    """
    An unclaimed pre-profile: one row of an institute's list, waiting for the
    student to claim it by proving control of the listed email. Pre-filled
    data is NEVER revealed before that proof (possession of a link or QR
    reveals nothing).
    """

    class Status(models.TextChoices):
        UNCLAIMED = "unclaimed", "Unclaimed"
        CLAIMED = "claimed", "Claimed"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    source_list = models.ForeignKey(
        UniversityStudentList, on_delete=models.CASCADE, related_name="students"
    )
    # Denormalized off source_list.institute_id purely so upload_list can
    # reconcile against a student's most recent row across ALL of an
    # institute's past lists without a join (see views.upload_list).
    institute_id = models.CharField(max_length=255, db_index=True)

    # Required list columns (spec section 2)
    full_name = models.CharField(max_length=255)
    email = models.CharField(max_length=255, db_index=True)
    field_of_study = models.CharField(max_length=255, blank=True, default="")
    degree_level = models.CharField(max_length=100, blank=True, default="")
    expected_graduation = models.CharField(max_length=20, blank=True, default="")  # MM/YYYY

    # Recommended columns
    phone = models.CharField(max_length=50, blank=True, default="")
    year_in_college = models.CharField(max_length=20, blank=True, default="")
    program_name = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=255, blank=True, default="")
    state = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNCLAIMED)
    claim_token = models.CharField(max_length=64, unique=True, default=_new_claim_token)
    # Last time an invite email carrying the claim link was sent; None if
    # never invited yet. Lets send_invites skip already-invited unclaimed
    # rows on a re-upload instead of re-spamming them every time.
    invited_at = models.DateTimeField(null=True, blank=True)

    # OTP state: only ever a hash at rest; short-lived; attempt-limited.
    otp_hash = models.CharField(max_length=128, blank=True, default="")
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_attempts = models.IntegerField(default=0)

    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_student_id = models.CharField(max_length=255, blank=True, default="")
    # Divergences recorded at confirm: [{field, list_value, student_value, at}]
    # -- countersigned, never silently overwritten (spec section 5).
    divergences = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_list", "email"], name="uniq_email_per_list"
            )
        ]

    def __str__(self):
        return f"ListedStudent({self.email}, {self.status})"
