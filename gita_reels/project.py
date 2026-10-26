"""A reel project on disk: the plan, every take of every shot, which take is selected, and the edit.

    reels/<id>/
        project.json          plan, review, options, selected takes, status, edit + validation results
        shots/01/t01/         one folder per take: keyframe.png, clip.mp4, qc/*.jpg, take.json
        clips/ audio/ titles/ timeline.* final.mp4   (written by assemble -> edit.py)

project.json is shared by the web UI and background jobs, so every change goes through update(), which
holds a lock file and re-reads before writing. take.json files are only written by the job making them.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .produce import ShotFailed, ShotResult, TakeMaker
from .settings import Settings
from .writer import ReelPlan, Shot

# Options a project may override on top of reels.yaml (UI controls).
REEL_OPTIONS = ("width", "height", "fps", "min_seconds", "max_seconds", "grade", "codec", "narration",
                "rotated_wide_shots", "match_shots")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "reel"


@contextmanager
def file_lock(path: Path, timeout: float = 20.0, stale: float = 60.0):
    t0 = time.time()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - t0 > timeout:
                raise TimeoutError(f"{path} is locked")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


class Project:
    def __init__(self, folder: Path, s: Settings):
        self.folder, self.base = Path(folder), s

    # --- storage ------------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.folder / "project.json"

    def meta(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, meta: dict) -> None:
        tmp = self.folder / "project.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, fn) -> dict:
        with file_lock(self.folder / ".lock"):
            meta = self.meta()
            fn(meta)
            self.write(meta)
            return meta

    @classmethod
    def create(cls, root: Path, name: str, meta: dict, s: Settings) -> "Project":
        root.mkdir(parents=True, exist_ok=True)
        folder = root / name
        n = 1
        while folder.exists():
            n += 1
            folder = root / f"{name}-{n}"
        folder.mkdir()
        meta = {"id": folder.name, "selected": {}, "rejected": {}, "prompt_overrides": {}, **meta}
        p = cls(folder, s)
        p.write(meta)
        return p

    # --- views --------------------------------------------------------------------------
    def settings(self) -> Settings:
        s = self.base.model_copy(deep=True)
        for k, v in (self.meta().get("options") or {}).items():
            if k in REEL_OPTIONS and v not in (None, ""):
                setattr(s.reel, k, type(getattr(s.reel, k))(v))
        if (self.folder / "characters.json").exists():
            s.paths.characters = str(self.folder / "characters.json")
        return s

    def plan(self) -> ReelPlan:
        return ReelPlan.model_validate(self.meta()["plan"])

    def shot_dir(self, i: int) -> Path:
        return self.folder / "shots" / f"{i + 1:02d}"

    def takes(self, i: int) -> list[dict]:
        out = []
        for tj in sorted(self.shot_dir(i).glob("t*/take.json")):
            try:
                out.append(json.loads(tj.read_text(encoding="utf-8")))
            except json.JSONDecodeError:   # being written right now
                continue
        return out

    def take(self, i: int, tid: str) -> dict:
        return json.loads((self.shot_dir(i) / tid / "take.json").read_text(encoding="utf-8"))

    def source(self, i: int, meta: dict | None = None) -> int:
        """The shot whose takes shot i uses: itself, or the shot named in its source_of."""
        j = (meta or self.meta())["plan"]["shots"][i].get("source_of", -1)
        return j if j is not None and j >= 0 and j != i else i

    def selected(self, i: int, meta: dict | None = None) -> dict | None:
        meta = meta or self.meta()
        i = self.source(i, meta)
        tid = meta["selected"].get(str(i))
        if not tid:
            return None
        t = self.take(i, tid)
        return t if t.get("status") == "ready" else None

    def _next_take_dir(self, i: int) -> Path:
        d = self.shot_dir(i)
        d.mkdir(parents=True, exist_ok=True)
        n = 1 + max((int(p.name[1:]) for p in d.glob("t[0-9]*") if p.name[1:].isdigit()), default=0)
        path = d / f"t{n:02d}"
        path.mkdir()
        return path

    # --- shot editing and takes -------------------------------------------------------------
    def edit_shot(self, i: int, fields: dict, overrides: dict | None = None) -> Shot:
        """Change a shot's spec (validated) and/or its prompt overrides. Existing takes keep their own copy."""
        from .cinema import normalize
        holder = {}

        def fn(meta):
            shot = Shot.model_validate({**meta["plan"]["shots"][i], **fields})
            normalize(shot, self.base)
            meta["plan"]["shots"][i] = shot.model_dump()
            if overrides is not None:
                clean = {k: v for k, v in overrides.items() if k in ("keyframe", "video", "negative") and v}
                meta["prompt_overrides"][str(i)] = clean
            holder["shot"] = shot
        self.update(fn)
        return holder["shot"]

    def make_take(self, i: int, providers: list, keyframer, claude, refs: dict[str, Path], hint: str = "",
                  seed: int | None = None) -> dict:
        meta = self.meta()
        shot = ReelPlan.model_validate(meta["plan"]).shots[i]
        overrides = meta["prompt_overrides"].get(str(i), {})
        # long enough for every later shot that cuts a second piece out of this clip (source_of)
        reuse = [sh["source_in"] + sh["on_screen_seconds"] + 0.3 for sh in meta["plan"]["shots"]
                 if sh.get("source_of", -1) == i and sh.get("source_in", -1) >= 0]
        folder = self._next_take_dir(i)
        return TakeMaker(self.settings(), keyframer, claude).make(i, shot, folder, providers, refs, hint=hint,
                                                                 overrides=overrides, seed=seed,
                                                                 seconds=max(reuse, default=0))

    def import_take(self, i: int, clip: Path, claude=None, note: str = "") -> dict:
        """Turn a clip made outside the pipeline into a take. Its first frame becomes the keyframe."""
        import shutil
        import subprocess

        from .produce import _now, frames_of, motion_metrics, save_take
        from .writer import qc_passes
        shot = self.plan().shots[i]
        folder = self._next_take_dir(i)
        dst = folder / ("clip" + (clip.suffix.lower() or ".mp4"))
        shutil.copyfile(clip, dst)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(dst), "-frames:v", "1",
                        str(folder / "keyframe.png")], check=True)
        take = {"id": folder.name, "shot": i, "created": _now(), "status": "ready", "provider": "manual",
                "model": "imported", "placeholder": False, "keyframe": "keyframe.png", "clip": dst.name,
                "source_file": clip.name, "note": note, "spec": shot.model_dump(), "qc": [], "cost_usd": 0.0,
                "prompts": {}, "metrics": motion_metrics(dst), "reviewed": False}
        if claude:
            q = claude.qc(frames_of(dst, folder / "qc"), [], shot)
            take["qc"].append({"stage": "clip", **q.model_dump()})
            take["reviewed"] = True
            if not qc_passes(q, self.base.claude.min_qc_score):
                take["status"] = "failed_qc"
        take["ended"] = _now()
        return save_take(folder, take)

    def review_sheet(self, i: int, tid: str) -> Path:
        """One image per take for a visual review: the keyframe plus frames from across the clip."""
        from PIL import Image

        from .produce import frames_of
        d = self.shot_dir(i) / tid
        t = self.take(i, tid)
        frames = [d / t["keyframe"]] + frames_of(d / t["clip"], d / "qc", n=4)
        imgs = [Image.open(p).convert("RGB") for p in frames]
        h = 480
        imgs = [im.resize((int(im.width * h / im.height), h)) for im in imgs]
        sheet = Image.new("RGB", (sum(im.width for im in imgs) + 8 * (len(imgs) + 1), h + 16), (12, 12, 12))
        x = 8
        for im in imgs:
            sheet.paste(im, (x, 8))
            x += im.width + 8
        out = d / "review.jpg"
        sheet.save(out, quality=88)
        return out

    def record_qc(self, i: int, tid: str, passed: bool, notes: list[str], reviewer: str = "claude-code") -> dict:
        """A visual review done outside the pipeline (the scheduled Claude Code task, or you)."""
        from .produce import save_take
        t = self.take(i, tid)
        t["qc"].append({"stage": "clip", "reviewer": reviewer, "passed": passed, "problems": notes})
        t["reviewed"] = True
        if t.get("status") in ("ready", "failed_qc"):
            t["status"] = "ready" if passed else "failed_qc"
        if not passed:
            t["retry_hint"] = "Fix: " + "; ".join(notes)
        save_take(self.shot_dir(i) / tid, t)
        return t

    def select(self, i: int, tid: str, force: bool = False) -> None:
        """Choose the take that goes in the edit. force=True lets a person overrule a failed visual QC."""
        from .produce import save_take
        t = self.take(i, tid)
        if t.get("status") == "failed_qc" and force:
            t.update(status="ready", qc_overridden_by_user=True)
            save_take(self.shot_dir(i) / tid, t)
        elif t.get("status") != "ready":
            raise ValueError(f"take {tid} of shot {i + 1} is not ready (status {t.get('status')})")

        def fn(meta):
            meta["selected"][str(i)] = tid
            meta["rejected"][str(i)] = [t for t in meta["rejected"].get(str(i), []) if t != tid]
        self.update(fn)

    def reject(self, i: int, tid: str) -> None:
        def fn(meta):
            if meta["selected"].get(str(i)) == tid:
                del meta["selected"][str(i)]
            meta["rejected"].setdefault(str(i), [])
            if tid not in meta["rejected"][str(i)]:
                meta["rejected"][str(i)].append(tid)
        self.update(fn)

    def auto_produce(self, providers: list, keyframer, claude, refs: dict[str, Path], max_retries: int) -> None:
        """Autopilot: for each shot without a usable selected take, make takes until one passes QC."""
        plan = self.plan()
        for i in range(len(plan.shots)):
            if self.source(i) != i or self.selected(i):
                continue
            hint, take = "", None
            for _ in range(max_retries + 1):
                take = self.make_take(i, providers, keyframer, claude, refs, hint=hint)
                if take["status"] == "ready":
                    self.select(i, take["id"])
                    break
                hint = take.get("retry_hint", "")
            else:
                last = take["qc"][-1]["problems"] if take.get("qc") else take.get("error")
                raise ShotFailed(f"shot {i + 1} failed QC after {max_retries + 1} attempts: {last}")

    # --- assembly -----------------------------------------------------------------------
    def missing(self) -> list[int]:
        meta = self.meta()
        return [i for i in range(len(meta["plan"]["shots"])) if not self.selected(i, meta)]

    def assemble(self) -> dict:
        from .edit import edit
        from .validate import validate_export
        missing = self.missing()
        if missing:
            raise ValueError("no selected take for shot(s) " + ", ".join(str(i + 1) for i in missing))
        meta, s = self.meta(), self.settings()
        plan = ReelPlan.model_validate(meta["plan"])
        results, placeholder = [], False
        for i in range(len(plan.shots)):
            t = self.selected(i, meta)
            d = self.shot_dir(self.source(i, meta)) / t["id"]
            placeholder |= bool(t.get("placeholder"))
            results.append(ShotResult(i, d / t["clip"], d / t["keyframe"], t.get("model", ""),
                                      t.get("gen_seconds", 0), 1, t.get("qc", [])))
        out = edit(plan, results, meta.get("verse"), s, self.folder)
        ends, t = [], 0.0
        for sh in plan.shots:
            t += sh.on_screen_seconds
            ends.append(t)
        black = [(e - sh.black_after, e) for sh, e in zip(plan.shots, ends) if sh.black_after > 0]
        report = validate_export(Path(out["final"]), s.reel.width, s.reel.height, s.reel.fps, out["seconds"],
                                 expect_audio=True, planned_black=black)
        from .review import review_final
        final_review = review_final(Path(out["final"]), self.folder / "review", plan)

        def fn(m):
            m["edit"] = out
            m["validation"] = report
            m["final_review"] = final_review
            m["takes_used"] = {str(i): m["selected"][str(self.source(i, m))] for i in range(len(plan.shots))}
            m["status"] = ("animatic" if placeholder else "ready") if report["ok"] else "failed_validation"
        return self.update(fn)
