from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class InstituteStudentListRenameMigrationTests(TransactionTestCase):
    reset_sequences = True

    migrate_from = ("institutes_list", "0002_roster_country_region")
    migrate_to = ("institutes_list", "0003_rename_universitystudentlist_institutestudentlist")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])

        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        Institute = old_apps.get_model("institutes", "Institute")
        UniversityStudentList = old_apps.get_model(
            "institutes_list", "UniversityStudentList"
        )
        ListedStudent = old_apps.get_model("institutes_list", "ListedStudent")

        institute = Institute.objects.create(
            name="Production Copy Institute",
            country="IN",
        )
        source_list = UniversityStudentList.objects.create(
            institute_id=institute.pk,
            contact_name="Admissions",
            contact_email="admissions@example.edu",
            row_count=1,
        )
        student = ListedStudent.objects.create(
            source_list_id=source_list.pk,
            institute_id=str(institute.uuid),
            full_name="Preserved Student",
            email="preserved@example.edu",
            country="IN",
            region="Maharashtra",
        )

        self.list_pk = source_list.pk
        self.student_pk = student.pk

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_rename_preserves_existing_roster_data(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_to])

        new_apps = self.executor.loader.project_state([self.migrate_to]).apps
        InstituteStudentList = new_apps.get_model(
            "institutes_list", "InstituteStudentList"
        )
        ListedStudent = new_apps.get_model("institutes_list", "ListedStudent")

        source_list = InstituteStudentList.objects.get(pk=self.list_pk)
        student = ListedStudent.objects.get(pk=self.student_pk)

        self.assertEqual(source_list.contact_email, "admissions@example.edu")
        self.assertEqual(student.source_list_id, self.list_pk)
        self.assertEqual(student.email, "preserved@example.edu")
        self.assertEqual(student.country, "IN")
        self.assertEqual(student.region, "Maharashtra")
