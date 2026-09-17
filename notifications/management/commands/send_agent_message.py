from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """
    Send a proactive, agent-initiated message to a student: it's written into
    their agent chat thread and push-notified, exactly like a live agent
    reply, but without the student having asked anything first.

    This is the manual/ops entry point for "the agent notifies the student
    on its own" -- any future scheduled job or in-graph agent action that
    needs to reach out proactively should call
    notifications.services.send_agent_message() directly instead of
    shelling out to this command; this command just exercises that same
    function for manual testing and one-off ops use.

    Usage:
        python manage.py send_agent_message <student_id> "<message>" [--title "..."]
    """

    help = "Send a proactive agent-initiated chat message + push notification to a student."

    def add_arguments(self, parser):
        parser.add_argument("student_id", help="The student's uuid (Account.student_uuid / StudentProfile.uuid).")
        parser.add_argument("message", help="Message text -- appears in chat and as the push notification body.")
        parser.add_argument("--title", default="New message from your agent", help="Push notification title.")

    def handle(self, *args, **options):
        from notifications.services import send_agent_message

        result = send_agent_message(
            student_id=options["student_id"],
            content=options["message"],
            title=options["title"],
        )
        if result is None:
            raise CommandError(f"No account found for student_id={options['student_id']!r}")

        self.stdout.write(self.style.SUCCESS(f"Queued NotificationLog #{result.id} for {options['student_id']!r}"))
