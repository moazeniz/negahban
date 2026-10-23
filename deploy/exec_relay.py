"""Authenticated websocket relay for the Liara exec API, to run on a host OUTSIDE Iran (e.g. Hetzner) behind a TLS terminator.
Why: the Liara exec gateway answers 'Sorry, connection failed' to sessions from Iranian networks (Liara's own runtime and
Arvan, verified 2026-10-22) while sessions from Finland work. Negahban (inside Liara) sets
  LIARA_EXEC_URL=wss://<relay-host>/v1/exec   LIARA_EXEC_RELAY_TOKEN=<shared secret>
The relay is a dumb pipe: it holds no Liara credential (the caller's own token travels inside the TLS-protected query string),
checks X-Relay-Token, optionally pins teamID and the allowed apps, caps session time and size, and logs one line per session
without the token or the command arguments.
Run: RELAY_TOKEN=... RELAY_TEAM_ID=... RELAY_APPS=demo-shop,bazargard-ai python deploy/exec_relay.py  (listens on 127.0.0.1:9311)"""
import asyncio, hmac, logging, os, time
from urllib.parse import parse_qs, urlsplit
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

UPSTREAM = os.environ.get('RELAY_UPSTREAM', 'wss://api.iran.liara.ir')
TOKEN = os.environ.get('RELAY_TOKEN', '')
TEAM = os.environ.get('RELAY_TEAM_ID', '')
APPS = {a.strip() for a in os.environ.get('RELAY_APPS', '').split(',') if a.strip()}
MAX_S, MAX_BYTES = int(os.environ.get('RELAY_MAX_SECONDS', '120')), 4 << 20
log = logging.getLogger('exec-relay')


def check(connection, request):
    u = urlsplit(request.path)
    q = parse_qs(u.query)
    if not TOKEN or not hmac.compare_digest(request.headers.get('X-Relay-Token', ''), TOKEN):
        return connection.respond(401, 'unauthorized\n')
    if u.path != '/v1/exec' or not q.get('token') or not q.get('project_id'):
        return connection.respond(404, 'not found\n')
    if TEAM and q.get('teamID', [''])[0] != TEAM:
        return connection.respond(403, 'team not allowed\n')
    if APPS and q['project_id'][0] not in APPS:
        return connection.respond(403, 'app not allowed\n')
    return None


async def handler(client):
    path = client.request.path
    q = parse_qs(urlsplit(path).query)
    app, cmd0 = q['project_id'][0], (q.get('cmd', [''])[0].split(' ') or [''])[0][:20]
    t0, sent = time.time(), 0
    try:
        async with connect(UPSTREAM + path, max_size=MAX_BYTES, open_timeout=15, ping_interval=None) as up:
            async def c2u():
                async for m in client:
                    await up.send(m)

            async def u2c():
                nonlocal sent
                async for m in up:
                    sent += len(m)
                    await client.send(m)
                    if sent > MAX_BYTES:
                        break
            tasks = [asyncio.ensure_future(c2u()), asyncio.ensure_future(u2c())]
            await asyncio.wait(tasks, timeout=MAX_S, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception as e:
        log.warning('app=%s cmd=%s error=%s', app, cmd0, type(e).__name__)
    finally:
        log.info('app=%s cmd=%s bytes=%d secs=%.1f', app, cmd0, sent, time.time() - t0)
        await client.close()


async def main():
    if not TOKEN:
        raise SystemExit('RELAY_TOKEN is required')
    host, port = os.environ.get('RELAY_HOST', '127.0.0.1'), int(os.environ.get('RELAY_PORT', '9311'))
    async with serve(handler, host, port, process_request=check, max_size=MAX_BYTES, ping_interval=None):
        log.info('exec relay on %s:%d -> %s', host, port, UPSTREAM)
        await asyncio.Future()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    asyncio.run(main())
