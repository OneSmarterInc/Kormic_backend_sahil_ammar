from django.core.management.base import BaseCommand, CommandError

from knowledge.vectors import enabled, sync_embeddings, _embedding_model
from universities.models import University


class Command(BaseCommand):
    help = "Download the local embedding model and index university facts in pgvector."

    def add_arguments(self, parser):
        parser.add_argument("--university-id", help="Only index this university UUID.")
        parser.add_argument("--download-model-only", action="store_true")

    def handle(self, *args, **options):
        if options["download_model_only"]:
            _embedding_model()
            self.stdout.write(self.style.SUCCESS("Embedding model is ready."))
            return
        if not enabled():
            raise CommandError("Indexing requires PostgreSQL with pgvector and UNIVERSITY_VECTOR_SEARCH=true.")
        universities = University.objects.all()
        if options["university_id"]:
            universities = universities.filter(uuid=options["university_id"])
            if not universities.exists():
                raise CommandError("University not found.")
        count = 0
        for university in universities.iterator():
            count += sync_embeddings(str(university.uuid))
        self.stdout.write(self.style.SUCCESS(f"Indexed {count} new or changed facts."))
