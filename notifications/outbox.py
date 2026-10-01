from datetime import timedelta
from django.db.models import F
from django.utils import timezone
from .models import PushDelivery
from .tasks import send_push_notification_task, check_push_receipts_task


def work_once():
    now = timezone.now()
    eligible = PushDelivery.objects.filter(available_at__lte=now, attempts__lt=4)
    job = eligible.order_by('available_at', 'pk').first()
    if job is None or not eligible.filter(pk=job.pk).update(
            available_at=now + timedelta(seconds=60), attempts=F('attempts') + 1):
        return False
    receipt_phase = bool(job.receipts)
    task = check_push_receipts_task if receipt_phase else send_push_notification_task
    result = task.apply(args=[job.receipts if receipt_phase else job.notification_id], throw=False)
    if result.successful():
        job.refresh_from_db()
        if receipt_phase or not job.receipts:
            job.delete()
    return True
