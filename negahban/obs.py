"""Observability standard (TEAM_BRIEF): one JSON object per log line on stdout with ts/level/app/env/release/event/
request_id/trace_id/span_id/msg (+ typed keys), request-id propagation (X-Request-ID, W3C traceparent), access logging as
`http.request`, and a global error handler that returns a friendly Persian JSON error with the request_id (no stacks)."""
import contextvars, json, logging, os, secrets, sys, time, traceback
from .redact import redact

APP = os.environ.get('SELF_APP', 'negahban-sre')
ENV = os.environ.get('APP_ENV', 'production')
RELEASE = os.environ.get('LIARA_RELEASE', os.environ.get('APP_VERSION', ''))
request_id = contextvars.ContextVar('request_id', default=None)
trace_id = contextvars.ContextVar('trace_id', default=None)
span_id = contextvars.ContextVar('span_id', default=None)
LEVEL = {'DEBUG': 'debug', 'INFO': 'info', 'WARNING': 'warn', 'ERROR': 'error', 'CRITICAL': 'critical'}


def _ts():
    t = time.time()
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + f'.{int(t % 1 * 1000):03d}Z'


class JsonFormatter(logging.Formatter):
    def format(self, r):
        d = {'ts': _ts(), 'level': LEVEL.get(r.levelname, 'info'), 'app': APP, 'env': ENV, 'release': RELEASE,
             'event': getattr(r, 'event', None) or ('app.server' if r.name.startswith('uvicorn') else 'log.' + r.name.split('.')[-1]), 'request_id': request_id.get(),
             'trace_id': trace_id.get(), 'span_id': span_id.get(), 'msg': redact(r.getMessage())[:2000]}
        for k, v in (getattr(r, 'fields', None) or {}).items():
            d[k] = redact(v) if isinstance(v, str) else v
        if r.exc_info:
            d['error_type'] = r.exc_info[0].__name__ if r.exc_info[0] else None
            d['stack'] = redact(''.join(traceback.format_exception(*r.exc_info)))[-4000:]
        return json.dumps({k: v for k, v in d.items() if v is not None and v != ''}, ensure_ascii=False, default=str)


def setup():
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    for n in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        lg = logging.getLogger(n); lg.handlers[:] = []; lg.propagate = True
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)  # replaced by our http.request events
    logging.getLogger('httpx').setLevel(logging.WARNING)


_log = logging.getLogger('negahban.events')


def event(name, level='info', msg='', **fields):
    _log.log({'debug': 10, 'info': 20, 'warn': 30, 'error': 40, 'critical': 50}[level], msg or name, extra={'event': name, 'fields': fields})


def new_ids(headers):
    rid = (headers.get('x-request-id') or '').strip()[:64] or secrets.token_hex(8)
    tp = headers.get('traceparent', '')
    parts = tp.split('-')
    tid = parts[1] if len(parts) == 4 and len(parts[1]) == 32 else secrets.token_hex(16)
    return rid, tid, secrets.token_hex(8)


def install(app):
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as SHTTPException

    @app.middleware('http')
    async def access(request: Request, call_next):
        rid, tid, sid = new_ids(request.headers)
        request.state.request_id = rid  # survives into exception handlers (which run outside this middleware)
        t1, t2, t3 = request_id.set(rid), trace_id.set(tid), span_id.set(sid)
        t0 = time.perf_counter()
        status = 500
        try:
            resp = await call_next(request)
            status = resp.status_code
            resp.headers['X-Request-ID'] = rid
            resp.headers['traceparent'] = f'00-{tid}-{sid}-01'
            return resp
        finally:
            path = request.url.path
            route = getattr(request.scope.get('route'), 'path', path)
            if not (path == '/healthz' or path.startswith('/static/')) or status >= 400:
                event('http.request', 'error' if status >= 500 else 'warn' if status >= 400 and status != 401 else 'info',
                      f'{request.method} {route} {status}', route=route, method=request.method, status=status,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1))
            request_id.reset(t1); trace_id.reset(t2); span_id.reset(t3)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logging.getLogger('negahban.app').error('unhandled error', exc_info=exc, extra={'event': 'app.error', 'fields': {'route': request.url.path, 'error_type': type(exc).__name__}})
        rid = getattr(request.state, 'request_id', None) or request_id.get() or secrets.token_hex(8)
        return JSONResponse({'detail': 'خطای داخلی پیش آمد؛ لطفاً دوباره تلاش کنید. اگر تکرار شد، شناسه‌ی درخواست را گزارش دهید.', 'request_id': rid},
                            status_code=500, headers={'X-Request-ID': rid})
