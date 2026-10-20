"""The studio: a local web UI for planning, generating, comparing and approving takes shot by shot.

Run `gita-reels studio` and open http://127.0.0.1:8765. Standard library only. Every expensive step
(planning, takes, assembly, reference analysis) runs as a separate `gita-reels` process, so the UI never
blocks and a crash in generation cannot take the UI down; job logs are kept under <out>/.jobs/.
Bound to localhost; mutating requests need an X-Studio header and a localhost Host, which blocks other
websites from driving it through your browser.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .settings import Settings

MAX_UPLOAD = 1024 * 1024 * 1024
STATIC = Path(__file__).with_name("ui_static")
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}


class Jobs:
    def __init__(self, root: Path, config: str):
        self.root, self.config = root, config
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self, kind: str, args: list[str], project: str = "", shot: int | None = None) -> dict:
        jid = uuid.uuid4().hex[:10]
        log = self.root / f"{jid}.log"
        job = {"id": jid, "kind": kind, "project": project, "shot": shot, "status": "running",
               "started": time.time(), "log": log.name}
        cmd = [sys.executable, "-m", "gita_reels.cli", "--config", self.config, "-v", *args]
        fh = open(log, "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=os.getcwd())
        job["pid"] = proc.pid
        with self.lock:
            self.items[jid] = job

        def wait():
            rc = proc.wait()
            fh.close()
            with self.lock:
                job.update(status="done" if rc == 0 else "failed", returncode=rc, ended=time.time())
                job["tail"] = log.read_text(encoding="utf-8", errors="replace")[-4000:]
        threading.Thread(target=wait, daemon=True).start()
        return job

    def list(self, project: str | None = None) -> list[dict]:
        with self.lock:
            items = [dict(j) for j in self.items.values() if project is None or j["project"] == project]
        for j in items:
            if j["status"] == "running":
                j["tail"] = (self.root / j["log"]).read_text(encoding="utf-8", errors="replace")[-1500:]
        return sorted(items, key=lambda j: j["started"], reverse=True)


def _safe_join(root: Path, rel: str) -> Path | None:
    p = (root / unquote(rel)).resolve()
    return p if p == root.resolve() or root.resolve() in p.parents else None


def make_handler(cfg: Settings, config_path: str, jobs: Jobs):
    from .cinema import keyframe_prompt, negative, presets, video_prompt
    from .pipeline import Studio
    from .project import Project
    from .providers import video_providers

    out_root = Path(cfg.paths.out)
    analysis_root = Path("analysis")
    studio = Studio(cfg)

    def project(pid: str) -> Project:
        if not re.fullmatch(r"[\w.-]+", pid) or not (out_root / pid / "project.json").exists():
            raise KeyError(pid)
        return Project(out_root / pid, cfg)

    def project_view(p: Project) -> dict:
        meta = p.meta()
        s = p.settings()
        provs = video_providers(cfg, lambda: None)
        default = (meta.get("options") or {}).get("provider") or next(iter(provs))
        style = getattr(provs.get(default), "style", "veo")
        from .writer import ReelPlan
        plan = ReelPlan.model_validate(meta["plan"]) if meta.get("plan") else None
        shots = []
        for i, sh in enumerate(plan.shots if plan else []):
            ov = meta.get("prompt_overrides", {}).get(str(i), {})
            shots.append({"spec": sh.model_dump(), "overrides": ov,
                          "composed": {"keyframe": keyframe_prompt(sh, s), "video": video_prompt(sh, s, style=style),
                                       "negative": negative(sh, s)},
                          "takes": p.takes(i), "selected": meta["selected"].get(str(i)),
                          "rejected": meta.get("rejected", {}).get(str(i), []),
                          "user_keyframe": (p.shot_dir(i) / "keyframe_user.png").exists()})
        return {"meta": meta, "shots": shots, "jobs": jobs.list(p.folder.name), "default_provider": default}

    def overview() -> dict:
        c = presets(cfg)
        provs = video_providers(cfg, lambda: None)
        return {
            "lenses": {k: v["label"] for k, v in c["lenses"].items()},
            "moves": {k: v["label"] for k, v in c["camera_moves"].items()},
            "lighting": {k: v["label"] for k, v in c["lighting"].items()},
            "focus": c["focus"], "grades": {k: v["label"] for k, v in c["grades"].items()},
            "providers": {n: {**p.caps.model_dump(), "issues": p.validate_config() + p.validate_credentials()}
                          for n, p in provs.items()},
            "episodes": [{"id": e["id"], "title": e["title"], "chapter": e["chapter"]} for e in cfg.episodes()],
            "aspects": list(ASPECTS), "reel": cfg.reel.model_dump(),
            "has_claude": bool(os.environ.get("ANTHROPIC_API_KEY")), "has_fal": bool(os.environ.get("FAL_KEY")),
        }

    def projects() -> list[dict]:
        rows = []
        for pj in sorted(out_root.glob("*/project.json"), reverse=True):
            try:
                m = json.loads(pj.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            rows.append({"id": pj.parent.name, "status": m.get("status"), "kind": m.get("kind"),
                         "title": (m.get("plan") or {}).get("title") or m.get("episode", {}).get("title", ""),
                         "created": m.get("created", "")})
        return rows

    def analyses() -> list[dict]:
        rows = []
        for rp in sorted(analysis_root.glob("*/report.json")):
            r = json.loads(rp.read_text(encoding="utf-8"))
            rows.append({"id": rp.parent.name, "source": r["source"], "shots": r["shot_count"],
                         "duration": r["duration"], "avg_shot_seconds": r["avg_shot_seconds"],
                         "md": (rp.parent / "report.md").read_text(encoding="utf-8")})
        return rows

    class Handler(BaseHTTPRequestHandler):
        server_version = "GitaStudio/1"

        def log_message(self, fmt, *a):  # quiet
            pass

        # --- helpers --------------------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _err(self, code: int, msg: str) -> None:
            self._json({"error": msg}, code)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ValueError("upload too large")
            return self.rfile.read(n) if n else b""

        def _data(self) -> dict:
            raw = self._body()
            return json.loads(raw) if raw else {}

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost", "[::1]", self.server.server_address[0])

        def _file(self, root: Path, rel: str) -> None:
            p = _safe_join(root, rel)
            if not p or not p.is_file():
                return self._err(404, "not found")
            size = p.stat().st_size
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            with open(p, "rb") as f:
                if rng and (rng.group(1) or rng.group(2)):
                    a = int(rng.group(1)) if rng.group(1) else max(0, size - int(rng.group(2)))
                    b = min(int(rng.group(2)), size - 1) if rng.group(1) and rng.group(2) else size - 1
                    f.seek(a)
                    data = f.read(b - a + 1)
                    self._send(206, data, ctype, {"Content-Range": f"bytes {a}-{b}/{size}", "Accept-Ranges": "bytes"})
                else:
                    self._send(200, f.read(), ctype, {"Accept-Ranges": "bytes"})

        # --- routes ---------------------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._err(403, "bad host")
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/api/overview":
                    return self._json(overview())
                if path == "/api/projects":
                    return self._json(projects())
                if m := re.fullmatch(r"/api/projects/([\w.-]+)", path):
                    return self._json(project_view(project(m.group(1))))
                if path == "/api/jobs":
                    return self._json(jobs.list())
                if path == "/api/analyses":
                    return self._json(analyses())
                if m := re.fullmatch(r"/files/projects/(.+)", path):
                    return self._file(out_root, m.group(1))
                if m := re.fullmatch(r"/files/analysis/(.+)", path):
                    return self._file(analysis_root, m.group(1))
                return self._err(404, "not found")
            except KeyError:
                return self._err(404, "no such project")

        def do_PUT(self):
            self.do_POST()

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Studio") != "1":
                return self._err(403, "forbidden")
            path = urlparse(self.path).path
            try:
                if path == "/api/projects":
                    return self._json(self._new(self._data()))
                if path == "/api/reference":
                    return self._json(self._reference())
                m = re.fullmatch(r"/api/projects/([\w.-]+)(?:/shots/(\d+))?/(\w+)", path)
                if not m:
                    return self._err(404, "not found")
                p, i, action = project(m.group(1)), int(m.group(2) or 0), m.group(3)
                n = len(p.meta()["plan"]["shots"])
                if m.group(2) is not None and not 0 <= i < n:
                    return self._err(400, "no such shot")
                if action == "spec":
                    d = self._data()
                    p.edit_shot(i, d.get("fields", {}), d.get("overrides"))
                    return self._json(project_view(p))
                if action == "take":
                    d = self._data()
                    args = ["take", str(p.folder), str(i + 1)]
                    if d.get("provider"):
                        args += ["--provider", d["provider"]]
                    if d.get("hint"):
                        args += ["--hint", d["hint"]]
                    return self._json(jobs.start("take", args, p.folder.name, i))
                if action in ("import", "keyframe"):
                    name = re.sub(r"[^\w.-]", "_", self.headers.get("X-Filename", "upload.bin"))[-80:]
                    up = p.folder / "uploads"
                    up.mkdir(exist_ok=True)
                    dst = up / f"{int(time.time())}-{name}"
                    dst.write_bytes(self._body())
                    if action == "keyframe":
                        from PIL import Image
                        p.shot_dir(i).mkdir(parents=True, exist_ok=True)
                        Image.open(dst).convert("RGB").save(p.shot_dir(i) / "keyframe_user.png")
                        return self._json(project_view(p))
                    return self._json(jobs.start("import", ["import-take", str(p.folder), str(i + 1), str(dst)],
                                                 p.folder.name, i))
                if action == "clearkeyframe":
                    (p.shot_dir(i) / "keyframe_user.png").unlink(missing_ok=True)
                    return self._json(project_view(p))
                if action == "select":
                    d = self._data()
                    p.select(i, d["take"], bool(d.get("force")))
                    return self._json(project_view(p))
                if action == "reject":
                    p.reject(i, self._data()["take"])
                    return self._json(project_view(p))
                if action == "assemble":
                    return self._json(jobs.start("assemble", ["assemble", str(p.folder)], p.folder.name))
                if action == "options":
                    d = self._data()
                    p.update(lambda meta: meta.setdefault("options", {}).update(d))
                    return self._json(project_view(p))
                return self._err(404, "unknown action")
            except KeyError as e:
                return self._err(404, f"not found: {e}")
            except (ValueError, TypeError) as e:
                return self._err(400, str(e))
            except Exception as e:  # validation errors from pydantic and friends
                return self._err(400, f"{type(e).__name__}: {e}")

        def _new(self, d: dict) -> dict:
            opts = dict(d.get("options") or {})
            if opts.get("aspect") in ASPECTS:
                w, h = ASPECTS[opts["aspect"]]
                scale = 720 / min(w, h) if opts.get("resolution") == "720p" else 1.0
                opts["width"], opts["height"] = int(round(w * scale / 2) * 2), int(round(h * scale / 2) * 2)
            args = ["new", "--planner", d.get("planner") or "auto"]
            if d.get("concept"):
                args += ["--concept", d["concept"], "--character", d.get("character", ""),
                         "--environment", d.get("environment", "")]
            elif d.get("episode"):
                args += ["--episode", d["episode"]]
            direction = d.get("direction", "")
            prefs = [f"prefer the {k} preset '{opts[k]}' where it suits the shot"
                     for k in ("lens", "move", "lighting") if opts.get(k)]
            if opts.get("motion"):
                prefs.append(f"overall degree of camera and subject motion: {opts['motion']}")
            if prefs:
                direction = "; ".join([direction] + prefs if direction else prefs)
            if direction:
                args += ["--direction", direction]
            for k in ("width", "height", "min_seconds", "max_seconds", "grade", "fps", "codec"):
                if opts.get(k) not in (None, ""):
                    args += ["--option", f"{k}={opts[k]}"]
            if opts.get("provider"):
                args += ["--option", f"provider={opts['provider']}"]
            return jobs.start("plan", args)

        def _reference(self) -> dict:
            name = re.sub(r"[^\w.-]", "_", self.headers.get("X-Filename", "reference.mp4"))[-80:]
            up = analysis_root / "uploads"
            up.mkdir(parents=True, exist_ok=True)
            dst = up / name
            dst.write_bytes(self._body())
            out = analysis_root / Path(name).stem
            args = ["analyze", str(dst), "--out", str(out)]
            if self.headers.get("X-Apply") == "1":
                args.append("--apply")
            return jobs.start("analyze", args)

    return Handler


def serve(cfg: Settings, config_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    jobs = Jobs(Path(cfg.paths.out) / ".jobs", config_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, config_path, jobs))
    print(f"Gita Studio running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
