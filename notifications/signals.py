from django.db.models.signals import post_save
from django.dispatch import receiver
from accounts.models import Account
from .services import notify_superusers


@receiver(post_save, sender=Account, dispatch_uid='notify_new_student_account')
def student_joined(sender, instance, created, raw=False, **kwargs):
    if raw or not created or instance.role != Account.Role.STUDENT:
        return
    student_id = instance.student_uuid
    name = instance.student_profile.name if student_id else instance.user.first_name
    notify_superusers(
        event_type='other', title='New student joined Kormic',
        body=f'{name or "A student"} has created a student account.',
        data={'type': 'student_joined', 'student_id': student_id,
              'route': f'/admin/students/{student_id}' if student_id else '/admin/students'},
        queue_email=True,
    )
