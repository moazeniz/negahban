"""Minimal MCP server (protocol 2025-06-18) exposing Negahban's tool registry.
Transports: stdio (`python -m negahban.mcp`) and streamable HTTP (POST /mcp, JSON responses, `Authorization: Bearer
$MCP_TOKEN`). Sensitive tools never execute here: they return an approval card id that the owner approves in the dashboard."""
import asyncio, json, secrets, sys
from .config import settings
from .sanitize import fence
from .tools import TOOLS, run

PROTOCOL = '2025-06-18'


def tool_list():
    out = []
    for t in TOOLS.values():
        out.append({'name': t['name'], 'description': t['desc'] + (' (SENSITIVE: creates an approval card in the Negahban dashboard; not executed until the owner approves)' if t['risk'] == 'sensitive' else ''),
                    'inputSchema': t['schema'], 'annotations': {'readOnlyHint': t['risk'] != 'sensitive', 'destructiveHint': t['name'] in ('delete_app', 'scale_app', 'unset_env')}})
    return out


async def dispatch(msg):
    mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
    if mid is None:  # notification
        return None
    try:
        if method == 'initialize':
            res = {'protocolVersion': params.get('protocolVersion') or PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
                   'serverInfo': {'name': 'negahban-sre', 'version': '1.0.0'},
                   'instructions': 'Negahban: Liara SRE tools. Read tools are safe; sensitive tools only create approval cards for the owner.'}
        elif method == 'ping':
            res = {}
        elif method == 'tools/list':
            res = {'tools': tool_list()}
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments') or {}
            out = await run(name, args, {'origin': 'mcp', 'actor': 'mcp'})
            is_err = isinstance(out, dict) and bool(out.get('error'))
            if isinstance(out, dict) and out.get('proposal'):
                out['approve_at'] = f"https://{settings.self_app}.liara.run/#approvals"
            res = {'content': [{'type': 'text', 'text': fence(f'negahban:{name}', out, 30000)}], 'isError': is_err}
        else:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
        return {'jsonrpc': '2.0', 'id': mid, 'result': res}
    except Exception as e:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32000, 'message': str(e)[:300]}}


async def handle_http(request):
    from fastapi.responses import JSONResponse, Response
    from .security import rate
    tok = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    if not settings.mcp_token or not secrets.compare_digest(tok, settings.mcp_token):
        return JSONResponse({'error': 'unauthorized'}, status_code=401, headers={'WWW-Authenticate': 'Bearer'})
    rate('mcp', 60, 60)
    body = await request.json()
    if isinstance(body, list):
        out = [r for r in [await dispatch(m) for m in body] if r]
        return JSONResponse(out) if out else Response(status_code=202)
    r = await dispatch(body)
    return JSONResponse(r, headers={'Mcp-Session-Id': 'stateless'}) if r else Response(status_code=202)


async def stdio():
    from . import store
    store.init()
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        r = await dispatch(msg)
        if r:
            sys.stdout.write(json.dumps(r, ensure_ascii=False) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    asyncio.run(stdio())
