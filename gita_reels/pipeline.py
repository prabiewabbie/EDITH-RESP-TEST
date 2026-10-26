"""Plan -> review -> takes -> edit -> validate, and the autopilot that walks the Gita in order."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .cinema import PURPOSE_DEFAULTS, lint, normalize
from .fal import BudgetExceeded, Fal
from .produce import ensure_refs
from .project import Project, slug
from .providers import AnimaticImage, FalImage, LocalImageCommand, UserKeyframe, video_providers
from .settings import Settings
from .writer import Claude, PlanReview, ReelPlan, Shot

log = logging.getLogger(__name__)
MAX_ATTEMPTS_PER_EPISODE = 2


def _norm(s: str) -> str:
    return " ".join(s.strip().strip("\"'“”‘’").split()).lower()


def template_plan(s: Settings, episode: dict) -> ReelPlan:
    """Offline plan from the reference's shot pattern, for trying the pipeline without a Claude key.
    It is never reviewed, so it can be rendered as an animatic but never published."""
    bible, chars = s.style_bible(), s.characters()
    story = episode["story"]
    present = [k for k in chars if k in story.lower()][:2]
    purposes = ["hero_closeup", "wide", "macro_insert", "low_angle_detail", "prop_detail", "establishing"]
    secs = max(bible.get("avg_shot_seconds", 2.0), s.reel.min_seconds / len(purposes))
    shots = []
    for i, (purpose, beat) in enumerate(zip(purposes, bible.get("shot_scale_pattern", purposes))):
        lens, move, light, focus = PURPOSE_DEFAULTS[purpose]
        shots.append(Shot(
            purpose=purpose, characters=present if purpose in ("hero_closeup", "establishing") else [],
            subject=f"{beat}, for this story: {story}", environment=episode.get("environment") or story,
            composition="subject on a third with clear foreground, middle-ground and background layers",
            camera_height="low" if purpose in ("hero_closeup", "low_angle_detail") else "eye", lens=lens,
            focus=focus, move=move, move_detail="slow and deliberate", subject_motion="cloth and dust move in the wind",
            lighting=light, atmosphere="warm haze and drifting dust", materials="silk, gold, bronze, dust",
            end_frame="a stable, composed frame", screen_direction="left_to_right", on_screen_seconds=round(secs, 2),
            framing="vertical", transition_in="cut", sfx="", narration=""))
    return ReelPlan(title=episode["title"][:70], description=story[:300], tags=["cinematic"], music_mood="epic",
                    shots=shots, verse_shot_index=-1, verse_line="")


class Studio:
    def __init__(self, s: Settings, claude: Claude | None = None, fal: Fal | None = None):
        self.s = s
        self._claude = claude
        self._fal = fal

    @property
    def fal(self) -> Fal:
        self._fal = self._fal or Fal(self.s)
        return self._fal

    @property
    def claude(self) -> Claude:
        self._claude = self._claude or Claude(self.s)
        return self._claude

    def has_claude(self) -> bool:
        return self._claude is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))

    # --- state --------------------------------------------------------------------------
    def state(self) -> dict:
        p = Path(self.s.paths.state)
        return json.loads(p.read_text()) if p.exists() else {"next": 0, "attempts": {}, "history": []}

    def save_state(self, st: dict) -> None:
        Path(self.s.paths.state).write_text(json.dumps(st, indent=2))

    def episode(self, episode_id: str | None) -> dict:
        eps = self.s.episodes()
        if episode_id:
            return next(e for e in eps if e["id"] == episode_id)
        return eps[self.state()["next"] % len(eps)]

    def verse_for(self, episode: dict) -> dict | None:
        verses = self.s.verses()
        return next((verses[r] for r in episode.get("verses", []) if r in verses), None)

    # --- planning -----------------------------------------------------------------------
    def plan(self, episode: dict, direction: str = "", s: Settings | None = None
             ) -> tuple[ReelPlan, PlanReview, dict | None, list[str]]:
        verse = self.verse_for(episode)
        claude = self.claude
        if s is not None:
            claude.s = s
        review = None
        for _ in range(3):
            plan = claude.plan(episode, verse, feedback=review, direction=direction)
            plan, notes = self._fix(plan, verse, s or self.s)
            review = claude.review(plan, episode, verse)
            if review.verdict != "revise":
                break
        return plan, review, verse, notes

    def _fix(self, plan: ReelPlan, verse: dict | None, s: Settings | None = None) -> tuple[ReelPlan, list[str]]:
        """Deterministic guards the model must not be trusted with."""
        s = s or self.s
        r, notes = s.reel, []
        for i, sh in enumerate(plan.shots):
            sh.on_screen_seconds = min(4.0, max(0.4, sh.on_screen_seconds))
            if sh.source_of >= 0 and not (sh.source_of < len(plan.shots) and sh.source_of != i
                                          and plan.shots[sh.source_of].source_of < 0):
                notes.append(f"shot {i + 1}: source_of {sh.source_of + 1} is not a shot with its own takes; ignored")
                sh.source_of = -1
            if not r.narration:
                sh.narration = ""
            notes += [f"shot {i + 1}: {n}" for n in normalize(sh, s)]
        wide = [i for i, sh in enumerate(plan.shots) if sh.framing == "rotated_wide"]
        allow = r.rotated_wide_shots and r.height > r.width
        for i in (wide if not allow else wide[1:]):
            plan.shots[i].framing = "vertical"
        total = sum(sh.on_screen_seconds for sh in plan.shots)
        if total > r.max_seconds or total < r.min_seconds:  # scale shot lengths into the allowed range
            target = min(max(total, r.min_seconds), r.max_seconds)
            for sh in plan.shots:
                sh.on_screen_seconds = round(sh.on_screen_seconds * target / total, 2)
        # The verse line is shown only if it is a verbatim excerpt of the stored translation.
        if not (verse and plan.verse_line and _norm(plan.verse_line) in _norm(verse["translation"])
                and 0 <= plan.verse_shot_index < len(plan.shots)):
            plan.verse_line, plan.verse_shot_index = "", -1
        notes += lint(plan)
        return plan, notes

    def review_ok(self, review: PlanReview | dict | None) -> bool:
        if review is None:
            return False
        if isinstance(review, dict):
            review = PlanReview.model_validate(review)
        m = self.s.claude.min_review_score
        return review.verdict == "approve" and min(review.fidelity_score, review.respect_score) >= m

    # --- projects -----------------------------------------------------------------------
    def create(self, episode_id: str | None = None, brief: dict | None = None, options: dict | None = None,
               direction: str = "", planner: str = "auto", plan_file: Path | None = None,
               theme: dict | None = None) -> Project:
        """New project from a storyline episode, or from a free concept brief
        ({concept, character, environment}). planner: auto | claude | template; plan_file imports a plan
        written elsewhere (e.g. in a Claude chat from `gita-reels brief`)."""
        options = options or {}
        if theme:
            ep = {"kind": "theme", "id": theme["id"], "chapter": int(theme["verse"].split(".")[0]),
                  "title": theme["title"], "story": theme["brief"], "set_pieces": [], "verses": [theme["verse"]],
                  "series": theme.get("series", ""), "format": theme.get("format", "")}
        elif brief:
            ep = {"kind": "concept", "id": "concept", "chapter": 0, "title": brief["concept"][:60],
                  "story": brief["concept"], "character": brief.get("character", ""),
                  "environment": brief.get("environment", ""), "set_pieces": [], "verses": []}
        else:
            ep = {"kind": "episode", **self.episode(episode_id)}
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{ep['id'] if ep['kind'] in ('episode', 'theme') else slug(ep['title'])}"
        p = Project.create(Path(self.s.paths.out), name, {"kind": ep["kind"], "episode": ep, "options": options,
                                                          "direction": direction, "status": "planning",
                                                          "created": datetime.now().isoformat(timespec="seconds")},
                           self.s)
        if brief and brief.get("character"):
            chars = {**self.s.characters(), "lead": brief["character"]}
            (p.folder / "characters.json").write_text(json.dumps(chars, indent=1), encoding="utf-8")
        if theme and theme.get("characters"):
            # the reel's own anchors (master prompt Part 5), copied word for word into every prompt and sheet
            chars = {**self.s.characters(), **theme["characters"]}
            (p.folder / "characters.json").write_text(json.dumps(chars, indent=1), encoding="utf-8")
        s = p.settings()
        use_claude = planner == "claude" or (planner == "auto" and self.has_claude() and not plan_file)
        if plan_file:
            # A plan written elsewhere: by the scheduled Claude Code task on your plan (no API key), or pasted
            # from a Claude chat. {"plan": ..., "review": ...}; the review is optional but required to publish.
            raw = json.loads(Path(plan_file).read_text(encoding="utf-8"))
            plan, notes = self._fix(ReelPlan.model_validate(raw.get("plan", raw)), self.verse_for(ep), s)
            review = PlanReview.model_validate(raw["review"]) if raw.get("review") else None
            meta = {"plan": plan.model_dump(), "review": review.model_dump() if review else None,
                    "verse": self.verse_for(ep), "plan_notes": notes, "planner": raw.get("planner", "imported"),
                    "prompt_overrides": {str(k): v for k, v in (raw.get("prompt_overrides") or {}).items()},
                    "hold": bool(raw.get("hold", ep["kind"] != "episode")), "caption": raw.get("caption", ""),
                    "look": (theme or {}).get("look", ""),
                    "status": "planned" if review is None or self.review_ok(review) else "rejected_by_review"}
        elif use_claude:
            plan, review, verse, notes = self.plan(ep, direction, s)
            meta = {"plan": plan.model_dump(), "review": review.model_dump(), "verse": verse, "plan_notes": notes,
                    "planner": "claude", "claude_usd": round(self.claude.cost_usd(), 3)}
            meta["status"] = "planned" if self.review_ok(review) else "rejected_by_review"
        else:
            plan, notes = self._fix(template_plan(s, ep), None, s)
            meta = {"plan": plan.model_dump(), "review": None, "verse": None, "plan_notes": notes,
                    "planner": "template", "status": "planned"}
        meta["estimate_usd"] = {"fal": self.estimate(plan)}
        p.update(lambda m: m.update(meta))
        return p

    def estimate(self, plan: ReelPlan, retry_factor: float = 1.3) -> float:
        models = {vm.id: vm for vm in self.s.fal.video_models}
        default = next((vm for vm in self.s.fal.video_models if vm.chain), self.s.fal.video_models[0])
        def need(i, sh):
            return max([sh.on_screen_seconds + 0.8] + [r.source_in + r.on_screen_seconds + 0.3 for r in plan.shots
                                                       if r.source_of == i and r.source_in >= 0])
        own = [(i, sh) for i, sh in enumerate(plan.shots) if sh.source_of < 0]
        video = sum(models.get(sh.model, default).cost(need(i, sh)) for i, sh in own)
        music = self.s.fal.music_price if plan.cue_sheet and self.s.fal.music_model else 0.0
        return round((len(own) * self.s.fal.keyframe_price + video) * retry_factor + music, 2)

    def providers(self, name: str | None = None) -> list:
        """The provider chain for a take: one named provider, or every fal model in order (autopilot)."""
        all_ = video_providers(self.s, lambda: self.fal)
        if name:
            if name not in all_:
                raise ValueError(f"unknown provider {name}; choose from {sorted(all_)}")
            return [all_[name]]
        return [p for n, p in all_.items() if n != "animatic"]

    def keyframer(self, placeholder: bool, provider: str | None = None, project: Project | None = None,
                  i: int = 0):
        """Animatic card, your own uploaded keyframe, a local image command, or fal (in that order of cost)."""
        if placeholder:
            return AnimaticImage(self.s)
        if project is not None and (project.shot_dir(i) / "keyframe_user.png").exists():
            return UserKeyframe(project.shot_dir(i) / "keyframe_user.png")
        if provider == "local":
            if self.s.local.image_command:
                return LocalImageCommand(self.s)
            if not os.environ.get("FAL_KEY"):
                raise RuntimeError("local video needs a keyframe: set local.image_command in reels.yaml or upload "
                                   "a keyframe for this shot")
        return FalImage(self.s, lambda: self.fal)

    def take(self, project: Project, i: int, provider: str | None = None, hint: str = "") -> dict:
        """One new take of one shot (UI / CLI). Never touches other takes."""
        chain = self.providers(provider)
        placeholder = bool(chain[0].caps.placeholder)
        s = project.settings()
        keyframer = self.keyframer(placeholder, provider, project, i)
        uses_fal = isinstance(keyframer, FalImage) or any(p.name in {v.id for v in self.s.fal.video_models}
                                                          for p in chain)
        refs = ensure_refs(s, self.fal, set(project.plan().shots[i].characters)) if uses_fal else {}
        if uses_fal:
            self.fal.start_reel()
        claude = self.claude if (self.has_claude() and not placeholder) else None
        before = claude.cost_usd() if claude else 0.0
        if claude:
            claude.s = s
        take = project.make_take(i, chain, keyframer, claude, refs, hint=hint)
        if claude:
            spent = claude.cost_usd() - before
            project.update(lambda m: m.update(claude_usd=round(m.get("claude_usd", 0) + spent, 3)))
        return take

    def import_take(self, project: Project, i: int, clip: Path, note: str = "") -> dict:
        """A clip you made yourself (any tool) becomes a take; Claude QC runs if a key is available."""
        claude = self.claude if self.has_claude() else None
        return project.import_take(i, clip, claude, note)

    # --- one reel, unattended -----------------------------------------------------------
    def make(self, episode_id: str | None = None, dry_run: bool = False) -> Path:
        project = self.create(episode_id, planner="claude")
        meta = project.meta()
        if meta["status"] != "planned" or dry_run:
            return project.folder
        s = project.settings()
        self.fal.start_reel()
        try:
            refs = ensure_refs(s, self.fal, {c for sh in project.plan().shots for c in sh.characters})
            project.update(lambda m: m.update(status="in_production"))
            project.auto_produce(self.providers(), self.keyframer(False), self.claude, refs,
                                 self.s.fal.max_retries_per_shot)
        except Exception as e:  # ShotFailed, BudgetExceeded, HTTP errors: keep what was made, mark failed
            project.update(lambda m: m.update(status="failed_production", error=str(e),
                                              fal_spent_usd=round(self.fal.reel_spent, 2)))
            return project.folder
        meta = project.assemble()
        ep, plan = meta["episode"], project.plan()
        project.update(lambda m: m.update(
            youtube=self._youtube_meta(plan, ep, m.get("verse"), m["edit"].get("music_credit", "")),
            fal_spent_usd=round(self.fal.reel_spent, 2), claude_usd=round(self.claude.cost_usd(), 3)))
        return project.folder

    def finish(self, project: Project) -> dict:
        """UI/CLI assembly: edit the selected takes, validate, and prepare the upload metadata."""
        meta = project.assemble()
        if meta.get("edit"):
            plan = project.plan()
            meta = project.update(lambda m: m.update(
                youtube=self._youtube_meta(plan, m["episode"], m.get("verse"), m["edit"].get("music_credit", ""),
                                    m.get("caption", ""))))
        return meta

    def _youtube_meta(self, plan: ReelPlan, ep: dict, verse: dict | None, credit: str, caption: str = "") -> dict:
        lines = [caption or plan.description, ""]
        if ep.get("kind") == "episode":
            lines.append(f"Bhagavad Gita, chapter {ep['chapter']}: {ep['title']}")
        if verse:
            lines.append(f"Bhagavad Gita {verse['chapter']}.{verse['verse']}. Translation: {verse['source']}")
        lines.append("Visuals are AI-generated.")
        if credit:
            lines.append(f"Music: {credit}")
        lines.append("#Shorts #BhagavadGita #Krishna")
        return {"title": plan.title, "description": "\n".join(lines), "tags": plan.tags}

    # --- split mode: Claude steps run by a scheduled Claude Code task, media by GitHub Actions ---
    def queued_plan(self, episode_id: str) -> Path | None:
        p = Path(self.s.paths.queue) / f"{episode_id}.json"
        return p if p.exists() else None

    def queued_theme(self) -> Path | None:
        """A queued standalone reel (queue/theme-*.json): it goes before the storyline and never moves it."""
        return next(iter(sorted(Path(self.s.paths.queue).glob("theme-*.json"))), None)

    def pending(self) -> Project | None:
        """The newest project still waiting for visual QC or more takes."""
        for pj in sorted(Path(self.s.paths.out).glob("*/project.json"), reverse=True):
            meta = json.loads(pj.read_text(encoding="utf-8"))
            if meta.get("status") in ("awaiting_qc", "in_production"):
                return Project(pj.parent, self.s)
        return None

    def held(self) -> Project | None:
        """The newest finished reel held for a person's go-ahead before it is uploaded."""
        for pj in sorted(Path(self.s.paths.out).glob("*/project.json"), reverse=True):
            meta = json.loads(pj.read_text(encoding="utf-8"))
            if meta.get("status") == "ready" and meta.get("hold") and not meta.get("published"):
                return Project(pj.parent, self.s)
        return None

    def produce(self) -> dict:
        """GitHub Actions, no Claude key: take the queued, reviewed plan (a theme reel first, else the next
        episode) and make one take per shot. Leaves the project 'awaiting_qc' for the scheduled task to review."""
        if (p := self.pending()) is not None:
            return {"status": "skipped", "reason": f"{p.folder.name} is still awaiting QC"}
        if theme_file := self.queued_theme():
            raw = json.loads(theme_file.read_text(encoding="utf-8"))
            theme = {"id": theme_file.stem, **raw["theme"]}
            project = self.create(plan_file=theme_file, planner="template", theme=theme,
                                  options=raw.get("options") or {})
            done = theme_file.parent / "done"
            done.mkdir(exist_ok=True)
            theme_file.replace(done / theme_file.name)
        else:
            ep = self.episode(None)
            plan_file = self.queued_plan(ep["id"])
            if not plan_file:
                return {"status": "skipped", "reason": f"no queued plan for {ep['id']}"}
            project = self.create(ep["id"], plan_file=plan_file, planner="template")
        if project.meta()["status"] != "planned" or not project.meta().get("review"):
            return {"status": "rejected", "folder": str(project.folder),
                    "reason": "plan has no approving review; it will not be produced"}
        self._takes_for_open_shots(project, hints={})
        return {"status": project.meta()["status"], "folder": str(project.folder),
                "fal_spent_usd": project.meta().get("fal_spent_usd")}

    def _takes_for_open_shots(self, project: Project, hints: dict[int, str]) -> None:
        s = project.settings()
        # the per-reel cap covers the whole reel across Actions runs, not one run
        self.fal.start_reel()
        already = project.meta().get("fal_spent_usd", 0.0)
        self.fal.reel_spent = already
        own = [i for i in range(len(project.plan().shots)) if project.source(i) == i]
        refs = ensure_refs(s, self.fal, {c for i in own for c in project.plan().shots[i].characters},
                           look=project.meta().get("look", ""))
        project.update(lambda m: m.update(status="in_production"))
        errors = []
        try:
            for i in own:
                if project.selected(i) or i not in hints and project.takes(i):
                    continue
                try:
                    t = project.make_take(i, self.providers(), self.keyframer(False), None, refs,
                                          hint=hints.get(i, ""))
                except BudgetExceeded:
                    raise
                except Exception as e:   # the take is recorded as failed; the other shots still get made
                    log.warning("shot %s failed: %s", i + 1, e)
                    errors.append(f"shot {i + 1}: {e}")
                    continue
                if t.get("clip"):
                    project.review_sheet(i, t["id"])     # what the scheduled task looks at
        except Exception as e:
            spent = round(self.fal.reel_spent, 2)
            project.update(lambda m: m.update(status="failed_production", error=str(e), fal_spent_usd=spent))
            raise
        spent = round(self.fal.reel_spent, 2)
        project.update(lambda m: m.update(status="awaiting_qc", fal_spent_usd=spent, take_errors=errors))

    def score_bed(self, project: Project) -> Path | None:
        """A scored reel's music bed, written to its cue sheet's sections by fal.music_model (once; cached)."""
        from .edit import bed_file
        from .score import bed_request
        plan = project.plan()
        if not (plan.cue_sheet and self.s.fal.music_model) or bed_file(project.folder):
            return bed_file(project.folder)
        total = sum(sh.on_screen_seconds for sh in plan.shots)
        self.fal.reel_spent = project.meta().get("fal_spent_usd", 0.0)
        res = self.fal.run(self.s.fal.music_model, bed_request(plan.cue_sheet, total), self.s.fal.music_price)
        url = res["audio"]["url"]
        path = self.fal.download(url, project.folder / "audio" / ("bed" + (Path(url.split("?")[0]).suffix or ".mp3")))
        spent = round(self.fal.reel_spent, 2)
        project.update(lambda m: m.update(fal_spent_usd=spent, music_bed={"model": self.s.fal.music_model,
                                                                            "request": bed_request(plan.cue_sheet, total)}))
        return path

    def finish_pending(self) -> dict:
        """After the scheduled task recorded its visual QC (gita-reels qc / select): retake failed shots with
        the reviewer's notes, or, when every shot has an approved take, assemble, validate and publish.
        A held reel (theme reels by default) stops at 'ready' until `release`."""
        from .publish import upload
        project = self.pending()
        if project is None:
            return {"status": "skipped", "reason": "nothing awaiting QC"}
        plan = project.plan()
        retakes = {}
        for i in range(len(plan.shots)):
            if project.source(i) != i or project.selected(i):
                continue
            takes = project.takes(i)
            last = takes[-1] if takes else None
            if last and last["status"] == "ready" and last.get("reviewed"):
                project.select(i, last["id"])
            elif last and last["status"] == "ready":
                return {"status": "awaiting_qc", "folder": str(project.folder), "reason": f"shot {i + 1} not reviewed"}
            elif len(takes) > self.s.fal.max_retries_per_shot:
                project.update(lambda m: m.update(status="failed_production",
                                                  error=f"shot {i + 1} failed QC {len(takes)} times"))
                ep = project.meta()["episode"]
                if ep.get("kind", "episode") == "episode":
                    self.advance(ep["id"], {"status": "failed_production", "folder": str(project.folder)})
                return {"status": "failed_production", "folder": str(project.folder)}
            else:
                retakes[i] = (last or {}).get("retry_hint", "")
        if retakes:
            self._takes_for_open_shots(project, retakes)
            return {"status": "awaiting_qc", "folder": str(project.folder), "retaken": [i + 1 for i in retakes],
                    "fal_spent_usd": project.meta().get("fal_spent_usd")}
        self.score_bed(project)
        meta = self.finish(project)
        ep = meta["episode"]
        result = {"status": meta["status"], "folder": str(project.folder),
                  "fal_spent_usd": meta.get("fal_spent_usd"), "final_review": meta.get("final_review")}
        if meta.get("hold"):
            result["held"] = "waiting for a person's go-ahead: dispatch reels.yml with command: release"
        elif meta["status"] == "ready" and self.s.publish.auto_publish and self.review_ok(meta.get("review")):
            try:
                result["url"] = upload(project.folder, self.privacy())
                result["status"] = "published"
            except Exception as e:
                result["publish_error"] = str(e)
        if result["status"] in ("published", "ready", "failed_validation") and ep.get("kind", "episode") == "episode":
            self.advance(ep["id"], result)
        return result

    def release(self) -> dict:
        """Upload the held reel after a person approved it. Privacy comes from REELS_PRIVACY or publish.privacy."""
        from .publish import upload
        project = self.held()
        if project is None:
            return {"status": "skipped", "reason": "no held reel is ready"}
        meta = project.meta()
        if not self.review_ok(meta.get("review")):
            return {"status": "refused", "reason": "the plan has no approving review"}
        url = upload(project.folder, self.privacy())
        project.update(lambda m: m.update(published={"url": url, "privacy": self.privacy(),
                                                     "at": datetime.now().isoformat(timespec="seconds")}))
        return {"status": "published", "url": url, "privacy": self.privacy(), "folder": str(project.folder)}

    def privacy(self) -> str:
        """REELS_PRIVACY (a workflow input) overrides publish.privacy for one run, e.g. a private test reel."""
        p = os.environ.get("REELS_PRIVACY", "").strip() or self.s.publish.privacy
        if p not in ("public", "unlisted", "private"):
            raise ValueError(f"bad privacy {p!r}")
        return p

    def advance(self, episode_id: str, result: dict) -> dict:
        """Record a finished (or abandoned) episode and move the storyline on; used by the scheduled task."""
        st = self.state()
        eps = [e["id"] for e in self.s.episodes()]
        st["attempts"][episode_id] = st["attempts"].get(episode_id, 0) + 1
        if eps[st["next"] % len(eps)] == episode_id:
            st["next"] += 1
        st["history"].append({"at": datetime.now().isoformat(timespec="seconds"), "episode": episode_id, **result})
        self.save_state(st)
        return st

    # --- autopilot ----------------------------------------------------------------------
    def autopilot(self) -> dict:
        from .publish import upload
        st = self.state()
        ep = self.episode(None)
        folder = self.make(ep["id"])
        meta = json.loads((folder / "project.json").read_text(encoding="utf-8"))
        result = {"episode": ep["id"], "folder": str(folder), "status": meta["status"]}
        if meta["status"] == "ready" and self.s.publish.auto_publish:
            try:
                result["url"] = upload(folder, self.s.publish.privacy)
                result["status"] = "published"
            except Exception as e:  # keep the finished reel; publishing can be retried by hand
                result["publish_error"] = str(e)
        tries = st["attempts"].get(ep["id"], 0) + 1
        st["attempts"][ep["id"]] = tries
        if result["status"] in ("published", "ready") or tries >= MAX_ATTEMPTS_PER_EPISODE:
            st["next"] += 1  # move on; a twice-failed episode is skipped and logged
        st["history"].append({"at": datetime.now().isoformat(timespec="seconds"), **result})
        self.save_state(st)
        return result
