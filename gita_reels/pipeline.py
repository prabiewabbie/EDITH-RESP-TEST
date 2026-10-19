"""Plan -> review -> footage -> edit -> package, and the autopilot that walks the Gita in order."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .fal import Fal
from .produce import Producer
from .settings import Settings
from .writer import Claude, PlanReview, ReelPlan

log = logging.getLogger(__name__)
MAX_ATTEMPTS_PER_EPISODE = 2


def _norm(s: str) -> str:
    return " ".join(s.strip().strip("\"'“”‘’").split()).lower()


class Studio:
    def __init__(self, s: Settings, claude: Claude | None = None, fal: Fal | None = None):
        self.s = s
        self.claude = claude or Claude(s)
        self._fal = fal

    @property
    def fal(self) -> Fal:
        self._fal = self._fal or Fal(self.s)
        return self._fal

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
        return next((verses[r] for r in episode["verses"] if r in verses), None)

    # --- planning -----------------------------------------------------------------------
    def plan(self, episode: dict) -> tuple[ReelPlan, PlanReview, dict | None]:
        verse = self.verse_for(episode)
        review = None
        for _ in range(3):
            plan = self.claude.plan(episode, verse, feedback=review)
            plan = self._fix(plan, verse)
            review = self.claude.review(plan, episode, verse)
            if review.verdict != "revise":
                break
        return plan, review, verse

    def _fix(self, plan: ReelPlan, verse: dict | None) -> ReelPlan:
        """Deterministic guards the model must not be trusted with."""
        r = self.s.reel
        for sh in plan.shots:
            sh.on_screen_seconds = min(4.0, max(1.2, sh.on_screen_seconds))
            if not r.narration:
                sh.narration = ""
        wide = [i for i, sh in enumerate(plan.shots) if sh.framing == "rotated_wide"]
        for i in (wide if not r.rotated_wide_shots else wide[1:]):
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
        return plan

    def review_ok(self, review: PlanReview) -> bool:
        m = self.s.claude.min_review_score
        return review.verdict == "approve" and min(review.fidelity_score, review.respect_score) >= m

    # --- one reel -----------------------------------------------------------------------
    def make(self, episode_id: str | None = None, dry_run: bool = False) -> Path:
        from .edit import edit
        ep = self.episode(episode_id)
        folder = Path(self.s.paths.out) / f"{datetime.now():%Y%m%d-%H%M}-{ep['id']}"
        folder.mkdir(parents=True, exist_ok=True)
        plan, review, verse = self.plan(ep)
        producer = Producer(self.s, self._fal if dry_run else self.fal, self.claude)
        meta = {"episode": ep, "verse": verse, "plan": plan.model_dump(), "review": review.model_dump(),
                "estimate_usd": {"fal": producer.estimate(plan)}, "status": "planned"}
        self._save(folder, meta)
        if not self.review_ok(review):
            meta["status"] = "rejected_by_review"
            return self._save(folder, meta)
        if dry_run:
            meta["claude_usd"] = round(self.claude.cost_usd(), 3)
            return self._save(folder, meta)
        try:
            results = producer.produce(plan, folder)
        except Exception as e:  # ShotFailed, BudgetExceeded, HTTP errors: keep what was made, mark failed
            meta.update(status="failed_production", error=str(e), fal_spent_usd=round(self.fal.reel_spent, 2))
            return self._save(folder, meta)
        meta["shots"] = [{"index": x.index, "model": x.model, "gen_seconds": x.gen_seconds, "attempts": x.attempts,
                          "qc": x.qc} for x in results]
        out = edit(plan, results, verse, self.s, folder)
        meta["edit"] = out
        meta["youtube"] = self._youtube_meta(plan, ep, verse, out.get("music_credit", ""))
        meta.update(status="ready", fal_spent_usd=round(self.fal.reel_spent, 2),
                    claude_usd=round(self.claude.cost_usd(), 3))
        return self._save(folder, meta)

    def _youtube_meta(self, plan: ReelPlan, ep: dict, verse: dict | None, credit: str) -> dict:
        lines = [plan.description, "", f"Bhagavad Gita, chapter {ep['chapter']}: {ep['title']}"]
        if verse:
            lines.append(f"Verse {verse['chapter']}.{verse['verse']} translation: {verse['source']}")
        lines.append("Visuals are AI-generated.")
        if credit:
            lines.append(f"Music: {credit}")
        lines.append("#Shorts #BhagavadGita #Krishna")
        return {"title": plan.title, "description": "\n".join(lines), "tags": plan.tags}

    @staticmethod
    def _save(folder: Path, meta: dict) -> Path:
        (folder / "project.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                                             encoding="utf-8")
        return folder

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
