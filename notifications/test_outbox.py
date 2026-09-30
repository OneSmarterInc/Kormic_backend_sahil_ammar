from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from accounts.models import Account
from notifications.models import NotificationLog, PushToken, PushDelivery
from notifications.services import notify_account
from notifications.outbox import work_once


@override_settings(AGENT_QUEUE_BACKEND='database', CELERY_TASK_ALWAYS_EAGER=False)
class PushOutboxTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(user=User.objects.create_user('push-test'), role='student')

    def enqueue(self):
        return notify_account(account=self.account, event_type='agent_reply', title='Reply', body='Test', queue_push=True)

    @patch('notifications.services.send_push_notification_task.delay')
    def test_chat_only_persists_outbox_without_contacting_broker(self, delay):
        log = self.enqueue()
        delay.assert_not_called()
        self.assertTrue(PushDelivery.objects.filter(notification=log).exists())
        self.assertTrue(work_once())
        log.refresh_from_db()
        self.assertEqual(log.status, 'skipped_no_token')
        self.assertFalse(PushDelivery.objects.exists())

    @patch('notifications.tasks.check_push_receipts_task.apply_async')
    @patch('notifications.expo.get_expo_push_receipts', return_value={})
    @patch('notifications.expo.send_expo_push_messages', return_value=[{'status': 'ok', 'id': 'receipt'}])
    def test_push_and_receipts_use_worker_without_celery(self, send, receipts, celery):
        PushToken.objects.create(account=self.account, token='ExponentPushToken[test]')
        log = self.enqueue()
        work_once()
        log.refresh_from_db()
        self.assertEqual(log.status, 'sent')
        celery.assert_not_called()
        self.assertFalse(work_once())
        PushDelivery.objects.update(available_at=timezone.now()-timedelta(seconds=1))
        work_once()
        receipts.assert_called_once_with(['receipt'])
        self.assertFalse(PushDelivery.objects.exists())

    @patch('notifications.expo.send_expo_push_messages', side_effect=RuntimeError('offline'))
    def test_failure_stays_queued_and_is_not_immediately_retried(self, send):
        PushToken.objects.create(account=self.account, token='ExponentPushToken[test]')
        self.enqueue()
        work_once()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(PushDelivery.objects.get().attempts, 1)
        self.assertFalse(work_once())
