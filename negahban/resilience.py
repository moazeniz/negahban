"""Shared retry + circuit-breaker helpers (TEAM_BRIEF standard): exponential backoff with full jitter, Retry-After on 429,
per-upstream breaker (open after N consecutive failures, half-open probe after a cool-down). Every attempt is logged as
`external.call`; breaker transitions as `circuit.change`."""
import random, time
from .obs import event


class CircuitOpen(Exception):
    def __init__(self, name, retry_in):
        super().__init__(f'circuit {name} open, retry in {retry_in:.0f}s')
        self.name, self.retry_in = name, retry_in


class Breaker:
    def __init__(self, name, threshold=5, cooldown=30):
        self.name, self.threshold, self.cooldown = name, threshold, cooldown
        self.fails, self.opened_at, self.state = 0, 0.0, 'closed'

    def check(self):
        if self.state == 'open':
            left = self.cooldown - (time.time() - self.opened_at)
            if left > 0:
                raise CircuitOpen(self.name, left)
            self._set('half_open')

    def ok(self):
        self.fails = 0
        if self.state != 'closed':
            self._set('closed')

    def fail(self):
        self.fails += 1
        if self.state == 'half_open' or self.fails >= self.threshold:
            self.opened_at = time.time()
            self._set('open')

    def _set(self, st):
        if st != self.state:
            event('circuit.change', 'warn' if st == 'open' else 'info', f'{self.name}: {self.state} -> {st}', upstream=self.name, circuit=st)
        self.state = st

    def info(self):
        return {'upstream': self.name, 'circuit': self.state, 'consecutive_failures': self.fails}


BREAKERS = {}


def breaker(name, **kw):
    if name not in BREAKERS:
        BREAKERS[name] = Breaker(name, **kw)
    return BREAKERS[name]


def backoff(attempt, base=0.8, cap=12.0, retry_after=None):
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return random.uniform(0, min(cap, base * 2 ** attempt))  # full jitter


def log_call(upstream, method, path, status, ms, attempt, max_attempts, error=None):
    event('external.call', 'warn' if (status == 0 or status >= 500 or error) else 'info', f'{upstream} {method} {path} -> {status}',
          upstream=upstream, method=method, route=path[:120], status=status, duration_ms=round(ms, 1), attempt=attempt,
          max_attempts=max_attempts, **({'error': str(error)[:200]} if error else {}))
