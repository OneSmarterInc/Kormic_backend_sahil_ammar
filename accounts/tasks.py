from celery import shared_task


@shared_task(ignore_result=True)
def cleanup_face_challenges():
    from django.core.management import call_command
    call_command('cleanup_face_challenges')
