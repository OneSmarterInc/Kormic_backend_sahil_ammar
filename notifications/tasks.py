from __future__ import annotations

import logging
from typing import Dict, List

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)

def _pending_statuses():
    from notifications.models import NotificationLog

    return (NotificationLog.Status.PENDING, NotificationLog.Status.FAILED)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_push_notification_task(self, log_id: int) -> None:
    """
    Deliver one NotificationLog to every active PushToken on its account via
    Expo. A token Expo reports as permanently dead (DeviceNotRegistered) is
    deactivated so future notifications stop trying it; a transient failure
    (network error, Expo 5xx) retries the whole task.
    """
    from notifications.expo import ExpoPushError, send_expo_push_messages
    from notifications.models import NotificationLog, PushToken

    try:
        log = NotificationLog.objects.get(id=log_id)
    except NotificationLog.DoesNotExist:
        logger.warning("send_push_notification_task: NotificationLog %s no longer exists", log_id)
        return

    if log.status not in _pending_statuses():
        logger.info(
            "send_push_notification_task: log %s already processed (status=%s), skipping",
            log_id, log.status,
        )
        return

    tokens = list(PushToken.objects.filter(account_id=log.account_id, is_active=True))
    if not tokens:
        log.status = NotificationLog.Status.SKIPPED_NO_TOKEN
        log.save(update_fields=["status", "updated_at"])
        return

    messages = [
        {
            "to": token.token,
            "title": log.title,
            "body": log.body,
            "data": log.data,
            "sound": "default",
        }
        for token in tokens
    ]

    try:
        tickets = send_expo_push_messages(messages)
    except (ExpoPushError, Exception) as exc:  # network errors, Expo 5xx, etc.
        log.status = NotificationLog.Status.FAILED
        log.error = str(exc)
        log.save(update_fields=["status", "error", "updated_at"])
        raise self.retry(exc=exc)

    receipt_map: Dict[str, str] = {}
    for token, ticket in zip(tokens, tickets):
        if ticket.get("status") == "error":
            details = ticket.get("details") or {}
            if details.get("error") == "DeviceNotRegistered":
                token.is_active = False
                token.last_error = ticket.get("message", "")
                token.save(update_fields=["is_active", "last_error", "updated_at"])
        elif ticket.get("id"):
            receipt_map[ticket["id"]] = token.token

    log.status = NotificationLog.Status.SENT
    log.save(update_fields=["status", "updated_at"])

    if receipt_map:
        # Expo's ticket response only confirms it accepted the message, not
        # that it was actually delivered -- receipts (checked after a short
        # delay, per Expo's own guidance) catch delivery-time failures like a
        # token that looked valid but the device was since unregistered.
        check_push_receipts_task.apply_async(args=[receipt_map], countdown=20)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def send_push_notifications_batch_task(self, log_ids: List[int]) -> None:
    """
    Same delivery as send_push_notification_task, batched across many
    NotificationLogs -- e.g. every student nudged by one proactive-checkin
    scan -- so N students cost ~N/100 Expo HTTP calls instead of N. Chunks
    internally at Expo's MAX_BATCH_SIZE (100 messages/request); a single
    call site does not need to worry about that cap.
    """
    from notifications.expo import MAX_BATCH_SIZE, ExpoPushError, send_expo_push_messages
    from notifications.models import NotificationLog, PushToken

    if not log_ids:
        return

    logs = {
        log.id: log
        for log in NotificationLog.objects.filter(id__in=log_ids, status__in=_pending_statuses())
    }
    if not logs:
        return

    tokens_by_account: Dict[int, List] = {}
    for token in PushToken.objects.filter(account_id__in={l.account_id for l in logs.values()}, is_active=True):
        tokens_by_account.setdefault(token.account_id, []).append(token)

    # Flatten to one (log, token) pair per outbound Expo message.
    entries = []
    no_token_log_ids = []
    for log in logs.values():
        tokens = tokens_by_account.get(log.account_id, [])
        if not tokens:
            no_token_log_ids.append(log.id)
            continue
        for token in tokens:
            entries.append((log, token))

    if no_token_log_ids:
        NotificationLog.objects.filter(id__in=no_token_log_ids).update(
            status=NotificationLog.Status.SKIPPED_NO_TOKEN
        )

    receipt_map: Dict[str, str] = {}
    retry_exc: Exception | None = None

    for i in range(0, len(entries), MAX_BATCH_SIZE):
        chunk = entries[i : i + MAX_BATCH_SIZE]
        messages = [
            {
                "to": token.token,
                "title": log.title,
                "body": log.body,
                "data": log.data,
                "sound": "default",
            }
            for log, token in chunk
        ]
        try:
            tickets = send_expo_push_messages(messages)
        except (ExpoPushError, Exception) as exc:  # network errors, Expo 5xx, etc.
            chunk_log_ids = {log.id for log, _ in chunk}
            NotificationLog.objects.filter(id__in=chunk_log_ids).update(
                status=NotificationLog.Status.FAILED, error=str(exc)
            )
            # Keep going through the remaining chunks (already-successful
            # ones above are committed as SENT below and won't be resent on
            # retry, since the re-run's status__in filter skips them) --
            # then retry the task once all chunks have been attempted, so
            # this one failed chunk doesn't block chunks after it.
            retry_exc = exc
            continue

        chunk_log_ids = set()
        for (log, token), ticket in zip(chunk, tickets):
            if ticket.get("status") == "error":
                details = ticket.get("details") or {}
                if details.get("error") == "DeviceNotRegistered":
                    token.is_active = False
                    token.last_error = ticket.get("message", "")
                    token.save(update_fields=["is_active", "last_error", "updated_at"])
            elif ticket.get("id"):
                receipt_map[ticket["id"]] = token.token
            chunk_log_ids.add(log.id)
        NotificationLog.objects.filter(id__in=chunk_log_ids).update(status=NotificationLog.Status.SENT)

    if receipt_map:
        check_push_receipts_task.apply_async(args=[receipt_map], countdown=20)

    if retry_exc is not None:
        raise self.retry(exc=retry_exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=15)
def check_push_receipts_task(self, receipt_map: Dict[str, str]) -> None:
    """receipt_map: {expo_receipt_id: push_token_string}."""
    from notifications.expo import ExpoPushError, get_expo_push_receipts
    from notifications.models import PushToken

    try:
        receipts = get_expo_push_receipts(list(receipt_map.keys()))
    except (ExpoPushError, Exception) as exc:
        raise self.retry(exc=exc)

    for receipt_id, receipt in receipts.items():
        if receipt.get("status") != "error":
            continue
        token_str = receipt_map.get(receipt_id)
        if not token_str:
            continue
        details = receipt.get("details") or {}
        if details.get("error") == "DeviceNotRegistered":
            PushToken.objects.filter(token=token_str).update(
                is_active=False, last_error=receipt.get("message", "")
            )


# Kept short and generous relative to the global 30s/25s default (see
# settings.CELERY_TASK_TIME_LIMIT) since a batch still does real per-student
# DB work (load_profile_data + compute_profile_intelligence), just bounded
# to PROACTIVE_CHECKIN_BATCH_SIZE students instead of the whole table.
PROACTIVE_CHECKIN_BATCH_SOFT_TIME_LIMIT = 4 * 60
PROACTIVE_CHECKIN_BATCH_HARD_TIME_LIMIT = 5 * 60


@shared_task
def run_proactive_checkins_task(cooldown_days: int | None = None) -> Dict[str, int]:
    """
    Scheduled scan (see CELERY_BEAT_SCHEDULE in settings.py) that lets the
    agent reach out on its own: for every student who hasn't been nudged
    within the cooldown window, check whether their profile has a genuine
    gap/weakness worth mentioning and, if so, message + push-notify them.

    Does no per-student work itself -- it only enumerates candidates and
    fans them out to run_proactive_checkin_batch_task in chunks of
    settings.PROACTIVE_CHECKIN_BATCH_SIZE. Previously this ran the whole
    scan as one synchronous loop, which at real student counts could run
    past the global CELERY_TASK_TIME_LIMIT and get SIGKILLed mid-loop, with
    everyone later in iteration order silently skipped. Fanning out bounds
    each sub-task's work and, as a side effect, lets each batch's pushes go
    out as ~1 Expo call instead of 1-per-student (see
    send_push_notifications_batch_task).
    """
    from accounts.models import Account
    from django_api.models import StudentProfile
    from notifications.proactive import DEFAULT_COOLDOWN_DAYS

    if cooldown_days is None:
        cooldown_days = getattr(settings, "PROACTIVE_CHECKIN_COOLDOWN_DAYS", DEFAULT_COOLDOWN_DAYS)
    batch_size = getattr(settings, "PROACTIVE_CHECKIN_BATCH_SIZE", 50)

    # Only students who've actually engaged (have a real Account + have
    # touched their profile) are candidates -- an empty StudentProfile row
    # with no name has nothing worth commenting on and no account to reach.
    linked_profile_ids = Account.objects.filter(
        student_profile__isnull=False
    ).values_list("student_profile_id", flat=True)
    student_ids = [
        str(u)
        for u in StudentProfile.objects.exclude(name="")
        .filter(id__in=linked_profile_ids)
        .values_list("uuid", flat=True)
    ]

    batches = 0
    for i in range(0, len(student_ids), batch_size):
        batch = student_ids[i : i + batch_size]
        run_proactive_checkin_batch_task.delay(batch, cooldown_days)
        batches += 1

    logger.info(
        "run_proactive_checkins_task: dispatched %s batch(es) for %s students",
        batches, len(student_ids),
    )
    return {"dispatched_batches": batches, "students": len(student_ids)}


@shared_task(
    bind=True,
    soft_time_limit=PROACTIVE_CHECKIN_BATCH_SOFT_TIME_LIMIT,
    time_limit=PROACTIVE_CHECKIN_BATCH_HARD_TIME_LIMIT,
)
def run_proactive_checkin_batch_task(self, student_ids: List[str], cooldown_days: int) -> Dict[str, int]:
    """One chunk of run_proactive_checkins_task's fan-out -- builds/queues
    each student's nudge (without dispatching its push individually), then
    sends every push for the whole batch as one call to
    send_push_notifications_batch_task."""
    from notifications.proactive import run_checkin_for_student

    sent_log_ids = []
    skipped = 0
    for student_id in student_ids:
        log = run_checkin_for_student(student_id, cooldown_days=cooldown_days, queue_push=False)
        if log is not None:
            sent_log_ids.append(log.id)
        else:
            skipped += 1

    if sent_log_ids:
        send_push_notifications_batch_task.delay(sent_log_ids)

    logger.info(
        "run_proactive_checkin_batch_task: queued=%s skipped=%s", len(sent_log_ids), skipped
    )
    return {"queued": len(sent_log_ids), "skipped": skipped}
