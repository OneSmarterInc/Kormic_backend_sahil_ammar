import os

from celery import Celery
from celery.signals import task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kormic_backend.settings")

app = Celery("kormic_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.autodiscover_tasks(["pure_multi_agent"])


# Celery never fires Django's request_finished signal (that's HTTP-cycle
# only), so nothing normally closes a worker's DB connection once it's
# past DATABASES['default']['CONN_MAX_AGE'] or gone stale (Postgres
# restart/RDS failover). close_old_connections() closes any connection
# that's expired or unusable so Django transparently opens a fresh one on
# next use, instead of a task blowing up with OperationalError. Hooked on
# both sides of every task run -- prerun so a task never starts on a dead
# connection, postrun so a long-idle worker between tasks doesn't hold one
# open past its CONN_MAX_AGE.
@task_prerun.connect
@task_postrun.connect
def _close_old_db_connections(**kwargs):
    from django.db import close_old_connections

    close_old_connections()
