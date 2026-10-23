"""Tiny in-process pub/sub that feeds the dashboard's Server-Sent Events stream."""
import asyncio, json, time
from collections import deque
from .redact import redact

_subs = set()
recent = deque(maxlen=300)


def publish(kind, **data):
    ev = {'kind': kind, 'ts': time.time(), **data}
    payload = redact(json.dumps(ev, ensure_ascii=False, default=str))
    recent.append(payload)
    for q in list(_subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
    return ev


def subscribe():
    q = asyncio.Queue(maxsize=500)
    _subs.add(q)
    return q


def unsubscribe(q):
    _subs.discard(q)
