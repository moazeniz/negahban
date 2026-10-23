"""Auth (single owner password from env), signed session cookies, CSRF double-submit, and rate limiting."""
import hashlib, hmac, secrets, time
from collections import defaultdict, deque
from fastapi import HTTPException, Request
from .config import settings

COOKIE = 'ngh_session'
CSRF_COOKIE = 'ngh_csrf'
TTL = 12 * 3600
_hits = defaultdict(deque)


def _sign(payload):
    return hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session():
    sid = secrets.token_urlsafe(18)
    exp = int(time.time()) + TTL
    payload = f'{sid}.{exp}'
    return f'{payload}.{_sign(payload)}', sid


def read_session(token):
    try:
        sid, exp, sig = token.split('.')
    except (AttributeError, ValueError):
        return None
    if not hmac.compare_digest(sig, _sign(f'{sid}.{exp}')) or int(exp) < time.time():
        return None
    return sid


def check_password(pw):
    want = settings.admin_password
    return bool(want) and hmac.compare_digest(hashlib.sha256((pw or '').encode()).digest(), hashlib.sha256(want.encode()).digest())


def client_ip(req: Request):
    xf = req.headers.get('x-forwarded-for', '')
    return (xf.split(',')[-1].strip() if xf else None) or (req.client.host if req.client else '?')  # last hop = added by Liara's proxy


def rate(key, limit, per, record=True):
    now = time.time()
    q = _hits[key]
    while q and q[0] < now - per:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, 'تعداد درخواست‌ها زیاد است؛ کمی صبر کنید')
    if record:
        q.append(now)


def record_hit(key):
    _hits[key].append(time.time())


def require(req: Request, csrf=False):
    sid = read_session(req.cookies.get(COOKIE, ''))
    if not sid:
        raise HTTPException(401, 'ابتدا وارد شوید')
    if csrf:
        c, h = req.cookies.get(CSRF_COOKIE, ''), req.headers.get('x-csrf-token', '')
        if not c or not hmac.compare_digest(c, h):
            raise HTTPException(403, 'توکن CSRF نامعتبر است؛ صفحه را تازه کنید')
        origin = req.headers.get('origin')
        if origin and req.headers.get('host') and origin.split('://', 1)[-1] != req.headers['host']:
            raise HTTPException(403, 'درخواست از مبدأ نامعتبر')
    return sid
