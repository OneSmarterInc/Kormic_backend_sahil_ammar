from django.test import SimpleTestCase
from pydantic import ValidationError
from pure_multi_agent.entry_validation import CompleteRequirement, PolicyDetails, validate_policy


class CompletenessTests(SimpleTestCase):
    def test_cgpa_requires_range_scale_and_scope(self):
        base = {'criterion': 'CGPA', 'detail': 'CGPA is required for admission', 'category': 'gpa', 'applies_to': 'all graduate applicants'}
        for missing in ({}, {'minimum': 7}, {'minimum': 7, 'maximum': 10}):
            with self.assertRaises(ValidationError):
                CompleteRequirement.model_validate({**base, **missing})
        result = CompleteRequirement.model_validate({**base, 'minimum': 7, 'maximum': 10, 'scale_maximum': 10})
        self.assertEqual(result.minimum, 7)

    def test_numeric_rule_cannot_be_mislabeled_as_other(self):
        with self.assertRaises(ValidationError):
            CompleteRequirement.model_validate({'criterion': 'GPA', 'detail': 'Some score required', 'category': 'other', 'applies_to': 'all applicants'})

    def test_score_bounds_invalid_and_missing_exam_are_rejected(self):
        base = {'criterion': 'IELTS', 'detail': 'IELTS requirement for admission', 'category': 'test_score', 'applies_to': 'all applicants', 'minimum': 6.5, 'maximum': 9, 'scale_maximum': 9}
        for more in ({}, {'test_name': 'IELTS Academic', 'minimum': 10}, {'test_name': 'IELTS Academic', 'maximum': 5}):
            with self.assertRaises(ValidationError):
                CompleteRequirement.model_validate({**base, **more})

    def test_scholarship_requires_all_terms(self):
        base = {'category': 'scholarship', 'applies_to': 'all applicants', 'effective_period': 'Fall 2027', 'amount': 5000}
        with self.assertRaises(ValidationError):
            PolicyDetails.model_validate(base)
        good = {**base, 'currency': 'USD', 'amount_basis': 'per academic year', 'eligibility': 'Admitted applicants with 3.5/4.0 GPA', 'application_process': 'Online scholarship application', 'deadline': '2027-01-31'}
        self.assertEqual(PolicyDetails.model_validate(good).currency, 'USD')
        for field in ('currency', 'eligibility', 'deadline', 'application_process', 'amount_basis'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PolicyDetails.model_validate({k: v for k, v in good.items() if k != field})

    def test_course_intake_and_deadline_incomplete(self):
        for category in ('course', 'intake', 'deadline'):
            with self.subTest(category=category), self.assertRaises(ValidationError):
                PolicyDetails.model_validate({'category': category, 'applies_to': 'all applicants', 'effective_period': '2027'})

    def test_placeholders_and_generic_scholarship_bypass_rejected(self):
        with self.assertRaises(ValidationError):
            PolicyDetails.model_validate({'category': 'general', 'applies_to': 'TBD', 'effective_period': 'ongoing'})
        with self.assertRaises(ValueError):
            validate_policy('Merit scholarship', {'category': 'general', 'applies_to': 'all students', 'effective_period': 'ongoing'})

    def test_blank_nested_details_and_nonfinite_values_are_rejected(self):
        for documents in ([' '], ['TBD']):
            with self.assertRaises(ValidationError):
                CompleteRequirement.model_validate({'criterion': 'Documents', 'detail': 'Documents required for admission', 'category': 'document', 'applies_to': 'all applicants', 'required_documents': documents})
        with self.assertRaises(ValidationError):
            PolicyDetails.model_validate({'category': 'tuition', 'applies_to': 'all students', 'effective_period': '2027', 'amount': float('inf'), 'currency': 'USD', 'amount_basis': 'per year'})
