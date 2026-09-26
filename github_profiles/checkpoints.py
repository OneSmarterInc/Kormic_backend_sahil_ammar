"""Synchronous LangGraph saver in the application's shared Django database.

The run-bound saver cannot read another user's thread. Every write is fenced by
the worker lease, including LangGraph's pending writes during crash recovery.
"""
import threading
import time
from functools import wraps
from django.db import connections, OperationalError
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from django_api.models import GitHubAgentCheckpoint, GitHubAgentWrite
from .scheduling import fenced


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            for attempt in range(6):
                try:
                    return method(self, *args, **kwargs)
                except OperationalError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt+1))
                finally:
                    # LangGraph performs persistence on helper threads. Do not
                    # leak one PostgreSQL connection per step on those threads.
                    if not connections['default'].in_atomic_block:
                        connections.close_all()
    return call


class DjangoSaver(BaseCheckpointSaver):
    def __init__(self, run, thread):
        super().__init__()
        self.run, self.thread = run, thread
        self.lock = threading.RLock()

    def rows(self, config):
        cfg = config['configurable']
        if cfg['thread_id'] != self.thread:
            raise ValueError('Checkpoint thread does not belong to this execution.')
        return GitHubAgentCheckpoint.objects.filter(run_id=self.run.pk, thread=self.thread,
            namespace=cfg.get('checkpoint_ns', ''))

    @serialized
    def get_tuple(self, config):
        rows = self.rows(config)
        checkpoint_id = config['configurable'].get('checkpoint_id')
        row = rows.filter(checkpoint_id=checkpoint_id).first() if checkpoint_id else rows.order_by('-checkpoint_id').first()
        if not row:
            return None
        cfg = {'configurable': {'thread_id': self.thread, 'checkpoint_ns': row.namespace, 'checkpoint_id': row.checkpoint_id}}
        parent = {'configurable': {**cfg['configurable'], 'checkpoint_id': row.parent_id}} if row.parent_id else None
        return CheckpointTuple(cfg, self.serde.loads_typed((row.payload_type, bytes(row.payload))), row.metadata, parent,
            [(w.task_id, w.channel, self.serde.loads_typed((w.payload_type, bytes(w.payload)))) for w in row.writes.order_by('task_id', 'index')])

    @serialized
    def put(self, config, checkpoint, metadata, new_versions):
        payload_type, payload = self.serde.dumps_typed(checkpoint)
        with fenced(self.run):
            self.rows(config)  # Validate the caller's thread before writing.
            GitHubAgentCheckpoint.objects.update_or_create(run_id=self.run.pk, thread=self.thread,
                namespace=config['configurable'].get('checkpoint_ns', ''), checkpoint_id=checkpoint['id'], defaults={
                'parent_id': config['configurable'].get('checkpoint_id', ''),
                'payload_type': payload_type, 'payload': payload, 'metadata': metadata})
        return {'configurable': {'thread_id': self.thread,
            'checkpoint_ns': config['configurable'].get('checkpoint_ns', ''), 'checkpoint_id': checkpoint['id']}}

    @serialized
    def put_writes(self, config, writes, task_id, task_path=''):
        with fenced(self.run):
            checkpoint = self.rows(config).get(checkpoint_id=config['configurable']['checkpoint_id'])
            for index, (channel, value) in enumerate(writes):
                payload_type, payload = self.serde.dumps_typed(value)
                values = {'checkpoint': checkpoint, 'task_id': task_id, 'index': WRITES_IDX_MAP.get(channel, index)}
                defaults = {'channel': channel, 'payload_type': payload_type, 'payload': payload}
                if channel in WRITES_IDX_MAP:
                    GitHubAgentWrite.objects.update_or_create(**values, defaults=defaults)
                else:
                    GitHubAgentWrite.objects.get_or_create(**values, defaults=defaults)

    def list(self, config, *, filter=None, before=None, limit=None):
        rows = self.rows(config).order_by('-checkpoint_id')
        if before:
            rows = rows.filter(checkpoint_id__lt=before['configurable']['checkpoint_id'])
        if limit is not None:
            rows = rows[:limit]
        for row in rows:
            if not filter or all(row.metadata.get(k) == v for k, v in filter.items()):
                yield self.get_tuple({'configurable': {'thread_id': self.thread, 'checkpoint_ns': row.namespace, 'checkpoint_id': row.checkpoint_id}})
