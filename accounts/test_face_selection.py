import numpy as np
from unittest.mock import patch
from django.test import SimpleTestCase

from accounts.face_engine import FaceScanError, measure, primary_face


class PrimaryFaceTests(SimpleTestCase):
    def test_wrong_pose_skips_recognition_and_never_returns_an_embedding(self):
        points = np.array([[100, 100], [200, 100], [150, 140], [120, 180], [180, 180]])
        with patch('accounts.face_engine.decode'), patch('accounts.face_engine.detect', return_value=points), patch('accounts.face_engine.embedding') as embed:
            with self.assertRaisesRegex(FaceScanError, 'requested head movement'):
                measure('synthetic', action='left')
            embed.assert_not_called()

    def test_smaller_background_faces_do_not_interrupt_the_subject(self):
        boxes = np.array([[30, 50, 90, 120], [200, 100, 440, 400], [475, 130, 565, 230]])
        self.assertEqual(primary_face(boxes, (640, 480)), 1)

    def test_background_detection_order_does_not_change_selection(self):
        foreground = [180, 90, 420, 390]
        for background in ([0, 0, 50, 70], [310, 20, 360, 80], [540, 390, 620, 470]):
            self.assertEqual(primary_face(np.array([foreground, background]), (640, 480)), 0)

    def test_comparable_foreground_faces_require_repositioning(self):
        with self.assertRaisesRegex(FaceScanError, 'other nearby faces'):
            primary_face(np.array([[180, 90, 420, 390], [400, 80, 600, 330]]), (640, 480))

    def test_a_small_center_face_is_not_selected_over_an_off_center_larger_face(self):
        with self.assertRaisesRegex(FaceScanError, 'near the center'):
            primary_face(np.array([[0, 70, 180, 430], [290, 180, 360, 270]]), (640, 480))

    def test_distant_single_face_still_requires_moving_closer(self):
        with self.assertRaisesRegex(FaceScanError, 'Move closer'):
            primary_face(np.array([[280, 180, 350, 260]]), (640, 480))

    def test_minor_position_change_during_turn_is_accepted(self):
        for offset in (-55, 0, 55):
            box = np.array([[200 + offset, 100, 440 + offset, 400]])
            self.assertEqual(primary_face(box, (640, 480)), 0)
