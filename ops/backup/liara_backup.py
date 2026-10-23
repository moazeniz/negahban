#!/usr/bin/env python3
"""Disk backups for Liara apps: native snapshots where the bundle has them, off-platform export everywhere.

  liara_backup.py run      -f infra/liara.yaml --out DIR [--app NAME]   what the daily timer runs
  liara_backup.py export   --app A --disk D --out DIR [--exclude GLOB]  copy a disk off-platform (file browser API)
  liara_backup.py native   {list|create|download} --app A --disk D [--name B] [--out DIR]
  liara_backup.py verify   ARCHIVE                                       integrity check (sha256 + SQLite + JSON)
  liara_backup.py prune    --dir DIR --keep N [--weekly W]               retention (newest N + newest per week for W weeks)
  liara_backup.py restore  ARCHIVE --app A --disk D [--prefix restore-<ts>] [--in-place] [--yes]
  liara_backup.py restore-native --app A --disk D --name B [--target-disk D2] [--yes]

Why two mechanisms
  * Liara's native disk backups exist only on the standard (daily, 7 kept) and pro bundles; basic has none.
    Three of our four apps run on basic, so the export path pulls each disk through the Liara file browser API
    (list -> createLink -> serve) to the monitoring host in Finland: a second provider, a second country.
  * Every archive carries MANIFEST.json (size + sha256 per file) and is verified right after it is written:
    each member is re-hashed, every *.db is opened and must pass `PRAGMA integrity_check`, every *.json parses.
  * A file-level copy of a live SQLite database can catch a write in flight; the verify step detects it and the
    retention keeps several generations. Apps that need point-in-time consistency should write a snapshot with
    `VACUUM INTO` to the disk before the window (see docs/OPERATIONS.md).
  * Restores are non-destructive by default: files go to a fresh `restore-<timestamp>/` folder on the disk and
    are swapped in by the operator with the app stopped (runbook). --in-place overwrites and needs --yes.
Exit codes: 0 ok, 1 failure, 3 verification failed.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
for cand in (HERE.parents[2] / "infra", HERE.parents[1] / "infra", Path.cwd() / "infra"):
    if (cand / "liara_api.py").exists():
        sys.path.insert(0, str(cand))
        break
from liara_api import Liara, LiaraError, bundle_caps, load_manifest, redact  # noqa: E402

CHUNK = 1 << 20


def log(msg: str) -> None:
    print(redact(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"), flush=True)


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def stream_to(url: str, dest: Path, headers: dict | None = None, timeout: float = 600) -> tuple[int, str]:
    h = hashlib.sha256()
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": "meowmeow-backup/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as out:
        while True:
            b = r.read(CHUNK)
            if not b:
                break
            out.write(b)
            h.update(b)
            n += len(b)
    return n, h.hexdigest()


class Disk:
    """One Liara disk: id lookup, file-browser host, native backup calls."""

    def __init__(self, li: Liara, app: str, disk: str):
        self.li, self.app, self.disk = li, app, disk
        self.project = li.project(app)
        self.pid = self.project["_id"]
        info = li.disks(self.pid)
        match = [d for d in info.get("disks", []) if d["name"] == disk]
        if not match:
            raise SystemExit(f"{app} has no disk named {disk!r}")
        self.meta = match[0]
        self.fb = (self.meta.get("filebrowserUrl") or "").rstrip("/")

    # -- file browser (export / restore) ---------------------------------------------
    def fb_call(self, method, op, params, raw=None, ctype=None):
        if not self.fb:
            raise SystemExit("disk has no filebrowserUrl; export is not available")
        q = urllib.parse.urlencode(params)
        return self.li.request(method, f"/v1/projects/{self.pid}/disks/{self.disk}/{op}?{q}", raw=raw, ctype=ctype,
                               base=self.fb, what=f"file browser {op}", timeout=600)

    def walk(self, path: str = "."):
        page, seen = 1, set()
        while page < 1000:
            items = [it for it in self.fb_call("GET", "list", {"path": path, "page": page}).get("items", [])
                     if it["path"] not in seen]
            if not items:  # empty page, or the server ignores `page` and repeats itself
                return
            for it in items:
                seen.add(it["path"])
                if it.get("isSymlink"):
                    continue
                if it.get("isDir"):
                    yield from self.walk(it["path"])
                else:
                    yield it
            page += 1

    def fetch(self, path: str, dest: Path) -> tuple[int, str]:
        link = self.fb_call("POST", "createLink", {"path": path, "type": "download", "expire": "1h"})
        qs = link.get("queries") or link
        url = f"{self.fb}/v1/projects/{self.pid}/disks/{self.disk}/serve?" + urllib.parse.urlencode(
            {"path": qs.get("path", path), "type": qs.get("type", "download"), "token": qs["token"]})
        return stream_to(url, dest)

    def upload(self, disk_path: str, data: bytes, overwrite: bool) -> None:
        b = "----meowbk" + uuid.uuid4().hex
        name = os.path.basename(disk_path)
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        self.fb_call("POST", "upload", {"path": os.path.dirname(disk_path) or ".", "overwrite": "yes" if overwrite else "no"},
                     raw=body, ctype="multipart/form-data; boundary=" + b)

    # -- native backups ------------------------------------------------------------------
    def caps(self):
        return bundle_caps(self.li.me(), self.project.get("planID", ""), self.project.get("bundlePlanID", ""))

    def native_list(self):
        return self.li.get(f"/v1/projects/{self.pid}/disks/{self.disk}/backups").get("backups", [])

    def native_create(self):
        if not self.caps().get("availableBackupInterval"):
            raise SystemExit(f"bundle {self.project.get('bundlePlanID')} has no native backups; use `export`")
        return self.li.post(f"/v1/projects/{self.pid}/disks/{self.disk}/backups", what="create backup")

    def native_download(self, name: str, dest: Path):
        link = self.li.post(f"/v1/projects/{self.pid}/disks/{self.disk}/backups/{name}/download", what="backup link")["link"]
        return stream_to(link, dest)


# -- archive build / verify -------------------------------------------------------------------
def export(li: Liara, app: str, disk: str, out: Path, exclude: list[str]) -> Path:
    d = Disk(li, app, disk)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{app}--{disk}--{stamp()}"
    work = Path(tempfile.mkdtemp(prefix=name, dir=out))
    files = []
    try:
        for it in d.walk("."):
            rel = it["path"][2:] if it["path"].startswith("./") else it["path"].lstrip("/")
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat) for pat in exclude):
                log(f"skip {rel} (excluded)")
                continue
            dest = work / "data" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            size, digest = d.fetch(it["path"], dest)
            if it.get("size") is not None and int(it["size"]) != size:
                log(f"warning: {rel} changed while copying ({it['size']} -> {size} bytes)")
            files.append({"path": rel, "size": size, "sha256": digest, "modifiedAt": it.get("modifiedAt")})
            log(f"copied {rel} {size} B")
        manifest = {"kind": "liara-disk-export", "version": 1, "app": app, "disk": disk, "projectID": d.pid,
                    "createdAt": datetime.now(timezone.utc).isoformat(), "excluded": exclude, "files": files,
                    "bytes": sum(f["size"] for f in files)}
        (work / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
        archive = out / f"{name}.tar.gz"
        with tarfile.open(archive.with_suffix(".tmp"), "w:gz") as tf:
            tf.add(work / "MANIFEST.json", arcname="MANIFEST.json")
            if (work / "data").exists():
                tf.add(work / "data", arcname="data")
        os.replace(archive.with_suffix(".tmp"), archive)
        os.chmod(archive, 0o600)
        log(f"wrote {archive} ({archive.stat().st_size} B, {len(files)} files)")
        return archive
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify(archive: Path) -> tuple[bool, list[str]]:
    report, ok = [], True
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                return False, [f"unsafe member {m.name!r}"]
        try:
            tf.extractall(tmp, filter="data")
        except TypeError:  # Python < 3.12 (members were vetted above)
            tf.extractall(tmp)  # noqa: S202
        man_p = Path(tmp) / "MANIFEST.json"
        if not man_p.exists():
            return False, ["MANIFEST.json missing"]
        man = json.loads(man_p.read_text())
        for f in man["files"]:
            p = Path(tmp) / "data" / f["path"]
            if not p.exists():
                ok = False
                report.append(f"MISSING {f['path']}")
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(CHUNK), b""):
                    h.update(b)
            if h.hexdigest() != f["sha256"]:
                ok = False
                report.append(f"HASH MISMATCH {f['path']}")
                continue
            if p.suffix in (".db", ".sqlite", ".sqlite3"):
                try:
                    con = sqlite3.connect(p)  # scratch copy: opening read-write lets SQLite replay a -wal file
                    try:
                        res = con.execute("PRAGMA integrity_check").fetchone()[0]
                        tables = con.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
                    finally:
                        con.close()
                except sqlite3.DatabaseError as e:
                    res, tables = f"error: {e}", 0
                if res != "ok":
                    ok = False
                report.append(f"{'ok ' if res == 'ok' else 'BAD'} sqlite {f['path']}: integrity_check={res} tables={tables}")
            elif p.suffix == ".json":
                try:
                    json.loads(p.read_text())
                    report.append(f"ok  json {f['path']}")
                except ValueError:
                    ok = False
                    report.append(f"BAD json {f['path']}")
        report.append(f"{len(man['files'])} files, {man.get('bytes', 0)} bytes, app={man.get('app')} disk={man.get('disk')} "
                      f"created={man.get('createdAt')}")
    return ok, report


def prune(directory: Path, keep: int, weekly: int = 4) -> list[Path]:
    """Keep the newest `keep` archives per app/disk plus the newest archive of each of the last `weekly` ISO weeks."""
    groups: dict[str, list[Path]] = {}
    for p in directory.glob("*--*--*.tar.gz"):
        groups.setdefault(p.name.rsplit("--", 1)[0], []).append(p)
    removed = []
    for _, items in groups.items():
        items.sort(key=lambda p: p.name, reverse=True)  # timestamps sort lexically
        keepers = set(items[:keep])
        weeks = {}
        for p in items:
            ts = datetime.strptime(p.name.rsplit("--", 1)[1][:16], "%Y%m%dT%H%M%SZ")
            weeks.setdefault(ts.isocalendar()[:2], p)
        keepers |= set(list(weeks.values())[:weekly])
        for p in items:
            if p not in keepers:
                p.unlink()
                removed.append(p)
    return removed


def restore(li: Liara, archive: Path, app: str, disk: str, prefix: str | None, in_place: bool, yes: bool) -> int:
    ok, rep = verify(archive)
    print("\n".join(rep))
    if not ok:
        log("refusing to restore an archive that fails verification")
        return 3
    d = Disk(li, app, disk)
    target = "." if in_place else (prefix or f"restore-{stamp()}")
    with tarfile.open(archive, "r:gz") as tf:
        man = json.load(tf.extractfile("MANIFEST.json"))
        plan = [(f["path"], f"{target}/{f['path']}" if target != "." else f["path"]) for f in man["files"]]
        print(f"restore {len(plan)} files from {archive.name} into {app}:{disk}/{target if target != '.' else '(in place)'}")
        if in_place:
            print("IN-PLACE restore overwrites live files: stop the app first (scale 0), restore, start it again.")
        if not yes:
            print("dry run (pass --yes to upload)")
            for src, dst in plan:
                print(f"  {src} -> {dst}")
            return 0
        for src, dst in plan:
            data = tf.extractfile(f"data/{src}").read()
            d.upload(dst, data, overwrite=in_place)
            log(f"uploaded {dst} ({len(data)} B)")
    return 0


# -- orchestration ----------------------------------------------------------------------------------
def run(li: Liara, manifest: dict, out: Path, only: str | None) -> int:
    failures = 0
    for app in manifest.get("apps", []):
        if only and app["name"] != only:
            continue
        for disk in app.get("disks", []):
            pol = disk.get("backup") or {}
            off = pol.get("offsite") or {}
            dest = out / app["name"]
            try:
                if pol.get("native"):
                    Disk(li, app["name"], disk["name"]).native_create()
                    log(f"{app['name']}:{disk['name']}: native backup requested")
                if off is not None and off is not False:
                    arc = export(li, app["name"], disk["name"], dest, off.get("exclude", []))
                    ok, rep = verify(arc)
                    for line in rep:
                        log(f"  {line}")
                    if not ok:
                        failures += 1
                        log(f"{arc.name}: VERIFY FAILED")
                    for p in prune(dest, int(off.get("keep", 7))):
                        log(f"pruned {p.name}")
            except (LiaraError, SystemExit, OSError) as e:
                failures += 1
                log(f"{app['name']}:{disk['name']}: backup failed: {e}")
    status = {"finishedAt": datetime.now(timezone.utc).isoformat(), "failures": failures}
    (out / "last-run.json").write_text(json.dumps(status))
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("-f", "--file", default="infra/liara.yaml")
    r.add_argument("--out", required=True)
    r.add_argument("--app")
    e = sub.add_parser("export")
    e.add_argument("--app", required=True)
    e.add_argument("--disk", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--exclude", action="append", default=[])
    n = sub.add_parser("native")
    n.add_argument("action", choices=["list", "create", "download"])
    n.add_argument("--app", required=True)
    n.add_argument("--disk", required=True)
    n.add_argument("--name")
    n.add_argument("--out", default=".")
    v = sub.add_parser("verify")
    v.add_argument("archive")
    p = sub.add_parser("prune")
    p.add_argument("--dir", required=True)
    p.add_argument("--keep", type=int, default=7)
    p.add_argument("--weekly", type=int, default=4)
    s = sub.add_parser("restore")
    s.add_argument("archive")
    s.add_argument("--app", required=True)
    s.add_argument("--disk", required=True)
    s.add_argument("--prefix")
    s.add_argument("--in-place", action="store_true")
    s.add_argument("--yes", action="store_true")
    rn = sub.add_parser("restore-native")
    rn.add_argument("--app", required=True)
    rn.add_argument("--disk", required=True)
    rn.add_argument("--name", required=True)
    rn.add_argument("--target-disk")
    rn.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "verify":
        ok, rep = verify(Path(a.archive))
        print("\n".join(rep))
        print("VERIFIED" if ok else "VERIFICATION FAILED")
        return 0 if ok else 3
    if a.cmd == "prune":
        for x in prune(Path(a.dir), a.keep, a.weekly):
            print(f"removed {x}")
        return 0
    li = Liara()
    if a.cmd == "run":
        return run(li, load_manifest(a.file), Path(a.out), a.app)
    if a.cmd == "export":
        arc = export(li, a.app, a.disk, Path(a.out), a.exclude)
        ok, rep = verify(arc)
        print("\n".join(rep))
        return 0 if ok else 3
    if a.cmd == "native":
        d = Disk(li, a.app, a.disk)
        if a.action == "list":
            for b in d.native_list():
                print(f"{b.get('name')}  {b.get('size')}  {b.get('lastModified')}")
        elif a.action == "create":
            print(json.dumps(d.native_create()))
        else:
            if not a.name:
                raise SystemExit("--name is required")
            dest = Path(a.out) / f"{a.app}--{a.disk}--{a.name}"
            size, digest = d.native_download(a.name, dest)
            print(f"{dest} {size} B sha256={digest}")
        return 0
    if a.cmd == "restore":
        return restore(li, Path(a.archive), a.app, a.disk, a.prefix, a.in_place, a.yes)
    if a.cmd == "restore-native":
        d = Disk(li, a.app, a.disk)
        body = {"backupName": a.name, "targetDiskName": a.target_disk or a.disk, "targetProjectID": d.pid}
        print(f"POST /v2/projects/{d.pid}/disks/{a.disk}/restore {json.dumps(body)}")
        if not a.yes:
            print("dry run (pass --yes to restore; restoring onto the live disk replaces its contents)")
            return 0
        print(json.dumps(li.post(f"/v2/projects/{d.pid}/disks/{a.disk}/restore", body, what="restore backup")))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
