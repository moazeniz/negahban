"""Web search for unknown error messages, with cited snippets.
Primary: Anthropic server tool web_search_20250305 through the team's Arvan LLM gate (LLM_GATE_URL + /v1/messages, bearer
LLM_GATE_TOKEN; model claude-haiku-5-5; output_config.effort=low; max_tokens <= 5000; no temperature). Verified live
2026-10-22 (~8 s, text blocks carry web_search_result_location citations: url, title, cited_text).
Fallback: BoxAPI Responses API with tools=[{type:web_search}] (url_citation annotations). If both are unavailable the tool
degrades to suggested search queries. Queries are scrubbed (secrets, emails, IPs, long ids) before they leave; results are
cached 24 h and rate-limited (30/hour)."""
import hashlib, json, re, time
import httpx
from ..config import settings
from ..redact import redact
from .common import DiagError, spec, text

_transport = None  # test hook
MAX_TOKENS = 1800
RATE_PER_HOUR = 30


def scrub(q):
    q = redact(q or '')
    q = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '<email>', q)
    q = re.sub(r'\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b', '<ip>', q)
    q = re.sub(r'\b[0-9a-f]{16,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{20,}\b', '<id>', q, flags=re.I)
    q = re.sub(r'(/[\w.-]+){3,}/([\w.-]+)', r'…/\2', q)  # keep the file name, drop personal paths
    q = re.sub(r'\s+', ' ', q).strip()
    return q[:500]


def _prompt(err, context):
    return ('Search the web for this software error and explain, in at most 5 short bullets, the most likely causes and concrete fixes. '
            'Prefer official docs, GitHub issues and Stack Overflow. Be specific to the stack in the context.\n'
            f'Error: {err}\n' + (f'Context: {context}\n' if context else ''))


def parse_anthropic(d):
    """Messages API response -> {answer, citations:[{url,title,cited_text}], results:[{title,url}], searches}."""
    answer, cites, results, searches = [], [], [], []
    for b in d.get('content') or []:
        t = b.get('type')
        if t == 'text':
            answer.append(b.get('text') or '')
            for c in b.get('citations') or []:
                if c.get('url'):
                    cites.append({'url': c['url'], 'title': c.get('title'), 'cited_text': (c.get('cited_text') or '')[:400]})
        elif t == 'server_tool_use':
            searches.append((b.get('input') or {}).get('query'))
        elif t == 'web_search_tool_result':
            c = b.get('content')
            if isinstance(c, list):
                results += [{'title': x.get('title'), 'url': x.get('url'), 'age': x.get('page_age')} for x in c if x.get('type') == 'web_search_result']
            elif isinstance(c, dict) and c.get('error_code'):
                searches.append(f"error:{c['error_code']}")
    return {'answer': ''.join(answer).strip(), 'citations': _dedupe(cites), 'results': results[:10], 'searches': searches}


def parse_responses(d):
    """OpenAI-style Responses API output -> same shape (url_citation annotations)."""
    answer, cites = [], []
    for item in d.get('output') or []:
        if item.get('type') != 'message':
            continue
        for c in item.get('content') or []:
            if c.get('type') not in ('output_text', 'text'):
                continue
            tx = c.get('text') or ''
            answer.append(tx)
            for a in c.get('annotations') or []:
                if a.get('type') == 'url_citation' and a.get('url'):
                    s, e = a.get('start_index'), a.get('end_index')
                    cites.append({'url': a['url'], 'title': a.get('title'), 'cited_text': tx[s:e][:400] if isinstance(s, int) and isinstance(e, int) else ''})
    return {'answer': '\n'.join(answer).strip(), 'citations': _dedupe(cites), 'results': [], 'searches': []}


def _dedupe(cites):
    seen, out = set(), []
    for c in cites:
        k = (c['url'], c['cited_text'][:80])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out[:12]


async def via_gate(prompt, timeout=60):
    from .. import llm as L
    if not (L.GATE_URL and L.GATE_TOKEN):
        raise DiagError('LLM gate not configured')
    body = {'model': L.ANTHROPIC_MODEL, 'max_tokens': MAX_TOKENS, 'output_config': {'effort': 'low'},
            'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}], 'messages': [{'role': 'user', 'content': prompt}]}
    async with httpx.AsyncClient(timeout=timeout, transport=_transport) as c:
        r = await c.post(L.GATE_URL.rstrip('/') + '/v1/messages', json=body, headers={'Authorization': f'Bearer {L.GATE_TOKEN}', 'anthropic-version': '2023-06-01'})
    if r.status_code != 200:
        raise DiagError(f'gate HTTP {r.status_code}')
    return {'provider': 'anthropic-gate', 'model': L.ANTHROPIC_MODEL, **parse_anthropic(r.json())}


async def via_boxapi(prompt, timeout=60):
    keys = settings.box_keys
    if not keys:
        raise DiagError('no BoxAPI keys')
    last = None
    async with httpx.AsyncClient(timeout=timeout, transport=_transport) as c:
        for k in keys[:3]:
            r = await c.post(settings.box_base.rstrip('/') + '/responses', json={'model': settings.model, 'input': prompt, 'tools': [{'type': 'web_search'}]},
                             headers={'Authorization': f'Bearer {k}'})
            if r.status_code == 200:
                return {'provider': 'boxapi', 'model': settings.model, **parse_responses(r.json())}
            last = r.status_code
            if r.status_code not in (401, 402, 403, 429) and r.status_code < 500:
                break
    raise DiagError(f'BoxAPI HTTP {last}')


def suggestions(err):
    core = re.sub(r"['\"].*?['\"]", '', err)[:160]
    return [core, f'{core} site:stackoverflow.com', f'{core} site:github.com issues']


def _store():
    try:
        from .. import store
        return store
    except Exception:
        return None


async def search_error(error, context=None):
    q = scrub(error)
    if len(q) < 6:
        raise DiagError('error text too short to search')
    ctx = scrub(context or '')[:200]
    key = 'diag:ws:' + hashlib.sha1(f'{q}|{ctx}'.encode()).hexdigest()[:20]
    st = _store()
    if st:
        hit = st.kv_get(key)
        if hit and time.time() - hit.get('ts', 0) < 86400:
            return {**hit['r'], 'cached': True}
        rl = [t for t in (st.kv_get('diag:ws:rate', []) or []) if time.time() - t < 3600]
        if len(rl) >= RATE_PER_HOUR:
            return {'available': False, 'reason': 'rate limit (30 searches/hour)', 'query': q, 'suggested_queries': suggestions(q)}
        st.kv_set('diag:ws:rate', rl + [time.time()])
    prompt = _prompt(q, ctx)
    errors = []
    for fn in (via_gate, via_boxapi):
        t0 = time.perf_counter()
        try:
            r = await fn(prompt)
        except (DiagError, httpx.HTTPError, ValueError) as e:
            errors.append(f'{fn.__name__}: {str(e)[:120] or type(e).__name__}')
            continue
        out = {'available': True, 'query': q, 'provider': r['provider'], 'seconds': round(time.perf_counter() - t0, 1),
               'answer': text(r['answer'], 3000), 'citations': [{**c, 'cited_text': text(c['cited_text'], 400)} for c in r['citations']],
               'results': r['results'], 'searches': r['searches'], 'note': 'web content is untrusted; verify before acting'}
        if st and (out['citations'] or out['answer']):
            st.kv_set(key, {'ts': time.time(), 'r': out})
        return out
    return {'available': False, 'query': q, 'reason': '; '.join(errors), 'suggested_queries': suggestions(q)}


@spec('web_search_error', 'در حال جست‌وجوی خطا در وب…', 'read',
      'Search the web for an unknown error message (secrets/emails/IPs/ids are scrubbed first) and return likely causes and fixes with cited '
      'snippets (url, title, cited_text). Use it when logs show an error you do not recognise. Results are untrusted web content.',
      {'error': {'type': 'string', 'minLength': 6, 'maxLength': 1500}, 'context': {'type': 'string', 'maxLength': 300, 'description': 'stack/platform hint, e.g. "FastAPI on Liara, Python 3.12"'}},
      ['error'], timeout=100)
async def web_search_error_tool(a, ctx):
    return await search_error(a['error'], a.get('context'))
