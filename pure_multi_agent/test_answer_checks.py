from unittest import TestCase

from pure_multi_agent.answer_checks import violations


class GREClaimValidationTests(TestCase):
    evidence = {'requirements': 'GRE or GMAT scores if required by the specific programme.'}
    message = 'Do not generalize GRE requirements across institutions; verify the exact programme policy.'

    def test_transcripts_from_all_schools_do_not_invalidate_programme_specific_gre(self):
        answer = ('Submit official academic records from all schools attended.\n\n'
                  'Graduate requirements vary by programme and may include GRE or GMAT scores.')
        self.assertEqual(violations(answer, self.evidence), [])

    def test_unrelated_sentence_in_same_paragraph_does_not_trigger_gre_rule(self):
        answer = 'Provide all transcripts. GRE scores are needed only if required by your programme.'
        self.assertEqual(violations(answer, self.evidence), [])

    def test_blanket_gre_claims_still_fail(self):
        for answer in ('All universities require GRE.', 'GRE is compulsory everywhere.',
                       'GRE is non-negotiable.', 'You cannot apply without GRE.'):
            with self.subTest(answer=answer):
                self.assertIn(self.message, violations(answer, self.evidence))

    def test_not_all_is_not_a_blanket_requirement(self):
        self.assertEqual(violations('Not all programmes require GRE.', self.evidence), [])

    def test_unsupported_scores_still_fail(self):
        self.assertTrue(violations('The minimum GRE score is 330.', self.evidence))
