from django.core.management.base import BaseCommand, CommandError

from django_api.retention import restore_test_is_recent, run_retention


class Command(BaseCommand):
    help = "Plan or apply conservative agent storage retention (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--restore-tested-backup-id", default="")
        parser.add_argument("--restore-tested-at", default="")
        parser.add_argument("--policy-approved", action="store_true")
        parser.add_argument("--max-rows", type=int, default=500)

    def handle(self, *args, **options):
        if options["apply"] and (not options["policy_approved"] or not restore_test_is_recent(
                options["restore_tested_backup_id"].strip(), options["restore_tested_at"].strip())):
            raise CommandError("Applying retention requires policy approval and a restore-tested backup dated within 30 days.")
        try:
            counts = run_retention(apply=options["apply"], max_rows=options["max_rows"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        mode = "applied" if options["apply"] else "dry-run"
        self.stdout.write(f"{mode}: {counts['jobs_compacted']} chat jobs compacted; "
                          f"{counts['jobs_skipped']} chat jobs skipped; "
                          f"{counts['github_checkpoints_deleted']} terminal GitHub checkpoints deleted")
        if options["apply"]:
            self.stdout.write(f"Restore-tested backup: {options['restore_tested_backup_id'].strip()}")
