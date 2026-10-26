from django.db import models


class CSPReport(models.Model):
    """Daily counters only; never persist browser payloads or credentials."""

    day = models.DateField()
    directive = models.CharField(max_length=40)
    blocked_origin = models.CharField(max_length=255)
    count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["day", "directive", "blocked_origin"], name="unique_daily_csp_report"
        )]
