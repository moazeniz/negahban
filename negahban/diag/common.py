"""Shared plumbing for the diagnostics toolkit: tool-spec helper, error type, output caps, redaction, audit, app locks."""
import asyncio, ipaddress, json, socket, time
from urllib.parse import urlsplit
from ..config import settings
from ..redact import redact, redact_obj
from ..sanitize import clean
from ..tools import ToolError

LOCKED_APPS = {'meowmeow-wallex', 'negahban-sre'}  # read-only diagnostics only, never approval-required commands
MAX_OUT = 24000  # chars of JSON per tool result


class DiagError(ToolError):
    """User-facing diagnostics error (subclass of the registry ToolError so tools.run() reports it cleanly)."""


def spec(name, label_fa, risk, desc, params=None, required=(), title=None, impact=None, timeout=60):
    """Decorator: attach a tool spec to a handler `async fn(args, ctx) -> dict`. Collected by diag.register()."""
    def deco(fn):
        fn._diag_spec = {'name': name, 'label_fa': label_fa, 'risk': risk, 'desc': desc, 'params': params or {}, 'required': list(required),
                         'title': title, 'impact': impact, 'timeout': timeout, 'fn': fn}
        return fn
    return deco


def specs_of(module):
    return [v._diag_spec for v in vars(module).values() if callable(v) and hasattr(v, '_diag_spec')]


def locked(app):
    """True if the app must only ever see read-only diagnostics (hard-coded locks + the owner's write_lock policy)."""
    if not app:
        return False
    if app in LOCKED_APPS or app == settings.self_app:
        return True
    try:
        from .. import store
        return bool(store.policy(app).get('write_lock'))
    except Exception:  # store unavailable (unit tests without a db) => only the hard-coded list applies
        return False


def audit(ctx, action, app, detail, ok=True):
    try:
        from .. import store
        store.audit((ctx or {}).get('actor') or 'agent', action, app, detail, ok)
    except Exception:
        pass


def text(s, limit=8000):
    """Untrusted text => cleaned (ANSI/invisible chars), secrets redacted, size-capped."""
    return redact(clean(s, limit))


def shrink(o, limit=MAX_OUT):
    """Recursively cap an object so its JSON stays under `limit` chars (lists are cut, long strings truncated)."""
    o = redact_obj(o)
    for str_cap, list_cap in ((4000, 200), (1500, 80), (600, 30), (250, 12), (120, 6)):
        s = json.dumps(o, ensure_ascii=False, default=str)
        if len(s) <= limit:
            return o
        o = _cut(o, str_cap, list_cap)
    s = json.dumps(o, ensure_ascii=False, default=str)
    return o if len(s) <= limit else {'truncated': True, 'preview': s[:limit - 200]}


def _cut(o, sc, lc):
    if isinstance(o, str):
        return o if len(o) <= sc else o[:sc] + f'…[+{len(o) - sc}]'
    if isinstance(o, dict):
        return {k: _cut(v, sc, lc) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        out = [_cut(x, sc, lc) for x in list(o)[:lc]]
        if len(o) > lc:
            out.append(f'…[{len(o) - lc} more]')
        return out
    return o


async def with_timeout(coro, seconds, what='operation'):
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        raise DiagError(f'{what} timed out after {seconds}s')


def now():
    return time.time()


# ---------------------------------------------------------------- SSRF guard for outbound probes
def _private(ip):
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified


async def check_public_url(url, allow_private=False):
    """Validate scheme/host and that every resolved address is public. Returns (host, port, [ips])."""
    u = urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise DiagError('url must be http(s)://host/...')
    if u.username or u.password:
        raise DiagError('credentials in URLs are not allowed')
    port = u.port or (443 if u.scheme == 'https' else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM), 8)
    except (OSError, asyncio.TimeoutError) as e:
        raise DiagError(f'DNS resolution failed for {u.hostname}: {type(e).__name__}')
    ips = sorted({i[4][0] for i in infos})
    if not allow_private and any(_private(ip) for ip in ips):
        raise DiagError(f'{u.hostname} resolves to a private/internal address; probes are limited to public endpoints')
    return u.hostname, port, ips
