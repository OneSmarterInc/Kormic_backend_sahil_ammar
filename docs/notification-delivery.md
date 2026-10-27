# Student and query notifications

- A newly created student account produces an inbox event and email for active platform superusers. Updating or logging into an existing account does not repeat it.
- Institute claim completion also emails the institute accounts that already receive the claim inbox event.
- Student questions notify the linked university account and staff assigned to the question's specific knowledge group by email and in-app. University questions notify the linked student by email and in-app. Unrelated departments and universities receive neither.
- Answers notify the original requesting party through the same channels. Superusers do not receive query or answer alerts.
- Student query events additionally create a durable push job. An inbox event remains available even without an installed app or push token.

Apply `python manage.py migrate` before restarting processes. Migration `notifications.0005_emaildelivery` adds the email outbox. No frontend changes or new packages are required.

Production uses the existing Celery beat `dispatch-agent-outbox` schedule and worker. Database-worker deployments use `invitation_worker`. Both drain the notification outbox; SMTP runs outside HTTP requests. Keep the applicable worker and scheduler running.

`NOTIFICATION_PORTAL_URL` defaults to `https://platform.kormic.ai`. Emails use the existing Django SMTP configuration and contain portal links, not login credentials. Web portal links preserve their hash-router paths.

Email failures retry up to five attempts with backoff. `EmailDelivery.sent_at` means the email backend accepted delivery, not that the recipient read it. `last_error` stores only the exception class. View delivery state in Django admin. Exhausted rows remain visible for investigation.

Concurrent workers lock each email row, and repeated query creation does not generate another inbox event. As with ordinary SMTP, a process failure after acceptance but before recording success can still produce a duplicate on retry; exactly-once external email delivery is not guaranteed.
