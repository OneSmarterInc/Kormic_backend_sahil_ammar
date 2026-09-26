"""Redis leases shared by web/agent workers; no process-local production limits."""
from contextlib import contextmanager
from functools import lru_cache, wraps
import time
import uuid

from django.conf import settings


class AgentBusy(RuntimeError):
    pass


class ResumeTurnLater(AgentBusy):
    """The graph checkpoint is safe to resume; contains only this turn's context."""
    def __init__(self, state, delay=10):
        super().__init__('Waiting for shared model capacity')
        self.state, self.delay = state, delay


@lru_cache(maxsize=4)
def _client(url):
    from redis import Redis
    return Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)


ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2])/1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) + 1)
return 1
"""

RATE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return count <= tonumber(ARGV[1]) and 1 or 0
"""


def check_rate(key, limit, seconds=60):
    if not settings.AGENT_DISTRIBUTED_LIMITS:
        return
    try:
        accepted = _client(settings.AGENT_REDIS_URL).eval(RATE, 1, "kormic:rate:" + key, limit, seconds)
    except Exception as exc:
        raise AgentBusy("Agent capacity service is unavailable.") from exc
    if not accepted:
        raise AgentBusy("Request rate exceeded. Please retry shortly.")


@contextmanager
def lease(key, limit=1, ttl=900, wait=0):
    if not settings.AGENT_DISTRIBUTED_LIMITS:
        if not (settings.DEBUG or settings.TESTING):
            raise RuntimeError("Distributed agent limits are required in production")
        yield
        return
    client = _client(settings.AGENT_REDIS_URL)
    token = str(uuid.uuid4())
    redis_key = "kormic:agent:" + key
    deadline = time.monotonic() + wait
    try:
        while not client.eval(ACQUIRE, 1, redis_key, limit, ttl, token):
            if time.monotonic() >= deadline:
                raise AgentBusy("Agent capacity is busy; please retry shortly.")
            time.sleep(0.1)
    except AgentBusy:
        raise
    except Exception as exc:
        raise AgentBusy("Agent capacity service is unavailable.") from exc
    try:
        yield
    finally:
        try:
            client.zrem(redis_key, token)
        except Exception:
            # Expiry reclaims the slot; never delete another owner's lease.
            pass


@contextmanager
def model_slot(university_id=None):
    from contextlib import ExitStack
    with ExitStack() as stack:
        if university_id:
            stack.enter_context(lease("university:" + str(university_id), settings.AGENT_UNIVERSITY_CONCURRENCY, ttl=300, wait=5))
        stack.enter_context(lease("claude", settings.AGENT_MODEL_CONCURRENCY, ttl=300, wait=5))
        check_rate("claude", settings.AGENT_MODEL_REQUESTS_PER_MINUTE)
        yield


def university_request(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        # Individual model calls have the global limit; this bounds complete
        # university executions, including retrieval and officer tool rounds.
        with lease("university-turn:" + self.university_id, settings.AGENT_UNIVERSITY_CONCURRENCY, ttl=900, wait=30):
            return fn(self, *args, **kwargs)
    return wrapped


class LimitedMessages:
    def __init__(self, messages):
        self.messages = messages

    def create(self, **kwargs):
        with model_slot():
            return self.messages.create(**kwargs)


def limited_client(client):
    client.messages = LimitedMessages(client.messages)
    return client
