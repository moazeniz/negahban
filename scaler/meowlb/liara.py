"""Async Liara API client (team-scoped) used by the replica controller. Token from LIARA_API_TOKEN, never logged."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import aiohttp

from .obs import redact


class LiaraError(RuntimeError):
    def __init__(self, what: str, status: int, payload):
        self.status, self.payload = status, payload
        super().__init__(f"{what}: HTTP {status} {redact(json.dumps(payload, default=str))[:300]}")


class Liara:
    def __init__(self, session: aiohttp.ClientSession, token: str | None = None, team: str | None = None, base: str | None = None):
        self.s = session
        self.token = token or os.getenv("LIARA_API_TOKEN", "")
        self.team = team if team is not None else os.getenv("LIARA_TEAM_ID", "")
        self.base = (base or os.getenv("LIARA_API_BASE") or "https://api.iran.liara.ir").rstrip("/")

    def _url(self, path: str) -> str:
        return self.base + path + (("&" if "?" in path else "?") + "teamID=" + self.team if self.team else "")

    async def req(self, method: str, path: str, body=None, data: bytes | None = None, ctype: str | None = None,
                  ok=(200, 201, 202, 204), what: str = "", timeout: float = 60):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            data, ctype = json.dumps(body).encode(), "application/json"
        if ctype:
            headers["Content-Type"] = ctype
        last = None
        for i in range(3 if method == "GET" else 1):
            try:
                async with self.s.request(method, self._url(path), data=data, headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    raw = await r.read()
                    try:
                        payload = json.loads(raw or b"null")
                    except ValueError:
                        payload = raw[:300].decode(errors="replace")
                    if r.status in ok:
                        return payload
                    last = LiaraError(what or f"{method} {path}", r.status, payload)
                    if method != "GET" or r.status < 500:
                        raise last
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = LiaraError(what or f"{method} {path}", 0, {"error": type(e).__name__})
            await asyncio.sleep(1.5 * (i + 1))
        raise last

    # -- reads ---------------------------------------------------------------------------
    async def projects(self) -> list[dict]:
        return (await self.req("GET", "/v1/projects", what="list apps")).get("projects", [])

    async def project(self, app: str) -> dict:
        return (await self.req("GET", f"/v1/projects/{app}", what=f"read {app}"))["project"]

    async def releases(self, app: str, count: int = 20) -> dict:
        return await self.req("GET", f"/v1/projects/{app}/releases?page=1&count={count}", what=f"releases of {app}")

    async def release(self, rid: str) -> dict:
        return (await self.req("GET", f"/v1/releases/{rid}", what="read release"))["release"]

    async def metrics(self, app: str) -> dict:
        return await self.req("GET", f"/v1/projects/{app}/metrics/summary", what=f"metrics of {app}")

    async def me(self) -> dict:
        return await self.req("GET", "/v1/me", what="account")

    # -- writes --------------------------------------------------------------------------
    async def create_app(self, name: str, plan: str, bundle: str, network_id: str | None) -> dict:
        body = {"name": name, "planID": plan, "platform": "docker", "bundlePlanID": bundle}
        if network_id:
            body["network"] = network_id
        return await self.req("POST", "/v1/projects", body, what=f"create {name}")

    async def resize(self, app: str, plan: str) -> dict:
        return await self.req("POST", f"/v1/projects/{app}/resize", {"planID": plan}, what=f"resize {app} to {plan}")

    async def delete_app(self, name: str) -> None:
        await self.req("DELETE", f"/v1/projects/{name}", what=f"delete {name}")

    async def source_bytes(self, release_id: str) -> bytes:
        link = (await self.req("POST", f"/v2/releases/{release_id}/source", what="source link"))["url"]
        async with self.s.get(link, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise LiaraError("download source", r.status, {})
            return await r.read()

    async def upload_source(self, app: str, archive: bytes) -> str:
        b = "----meowlb" + uuid.uuid4().hex
        data = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"source.tar.gz\"\r\n"
                f"Content-Type: application/gzip\r\n\r\n").encode() + archive + f"\r\n--{b}--\r\n".encode()
        r = await self.req("POST", f"/v2/projects/{app}/sources", data=data, ctype="multipart/form-data; boundary=" + b,
                           what=f"upload source to {app}", timeout=600)
        return r["sourceID"]

    async def create_release(self, app: str, body: dict) -> str:
        return (await self.req("POST", f"/v2/projects/{app}/releases", body, what=f"release {app}"))["releaseID"]
