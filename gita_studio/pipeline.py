"""Orchestration: create -> review -> (approve) -> schedule -> publish, plus the weekly strategy loop."""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import stages
from .config import Config
from .integrations.media import get_media_provider
from .integrations.publish import build_payload, get_publisher
from .llm import Claude, ClaudeRefused
from .models import Idea
from .scripture import Corpus
from .store import Store, now

log = logging.getLogger(__name__)
MAX_REVISIONS = 2


class Studio:
    def __init__(self, config: Config, claude: Claude | None = None, store: Store | None = None,
                 corpus: Corpus | None = None):
        self.config = config
        self.claude = claude or Claude(config)
        self.store = store or Store(config.paths.db)
        self.corpus = corpus or Corpus.load(config.paths.verses)

    # --- strategy -----------------------------------------------------------------------
    def refresh_strategy(self) -> dict | None:
        perf = self.store.performance()
        if len(perf) < 5:
            log.info("only %d posts with metrics; skipping strategy refresh", len(perf))
            return self.store.latest_strategy()
        brief = stages.plan_strategy(self.claude, self.config, perf).model_dump()
        self.store.save_strategy(brief)
        return brief

    # --- creation -----------------------------------------------------------------------
    def create_posts(self, count: int | None = None) -> list[int]:
        count = count or self.config.content.posts_per_day
        ideas = stages.generate_ideas(self.claude, self.config, self.corpus, count,
                                      self.store.used_verses(), self.store.latest_strategy())
        ids = []
        for idea in ideas:
            try:
                ids.append(self.produce(idea))
            except ClaudeRefused as e:
                log.warning("skipping idea %s.%s: %s", idea.chapter, idea.verse, e)
        return ids

    def produce(self, idea: Idea) -> int:
        verse = self.corpus.get(idea.chapter, idea.verse)
        if verse is None:
            raise ValueError(f"BG {idea.chapter}.{idea.verse} is not in the corpus")

        script = stages.write_script(self.claude, self.config, idea, verse)
        review = stages.review_script(self.claude, self.config, script, verse)
        for _ in range(MAX_REVISIONS):
            if review.verdict != "revise":
                break
            script = stages.write_script(self.claude, self.config, idea, verse, feedback=review)
            review = stages.review_script(self.claude, self.config, script, verse)

        package = {"idea": idea.model_dump(), "verse": verse.model_dump(), "script": script.model_dump(),
                   "review": review.model_dump()}
        passed = (review.verdict == "approve" and review.quote_is_exact
                  and review.fidelity_score >= self.config.review.min_fidelity_score
                  and review.respect_score >= self.config.review.min_fidelity_score)
        if not passed:
            return self.store.add_post(chapter=idea.chapter, verse=idea.verse, format=idea.format,
                                       angle=idea.angle, status="rejected", fidelity=review.fidelity_score,
                                       package=package, note="failed automated review")

        package["visuals"] = stages.plan_visuals(self.claude, self.config, idea, script).model_dump()
        package["distribution"] = stages.write_distribution(self.claude, self.config, verse, script).model_dump()
        status = "needs_review" if self.config.review.require_human_approval else "approved"
        post_id = self.store.add_post(chapter=idea.chapter, verse=idea.verse, format=idea.format, angle=idea.angle,
                                      status=status, fidelity=review.fidelity_score, package=package)
        package["media"] = get_media_provider(self.config).render(post_id, package)
        self.store.db.execute("UPDATE posts SET package = ? WHERE id = ?",
                              (json.dumps(package, ensure_ascii=False), post_id))
        self.store.db.commit()
        return post_id

    # --- human review -------------------------------------------------------------------
    def approve(self, post_id: int) -> None:
        self._require(post_id, {"needs_review"})
        self.store.set_status(post_id, "approved")

    def reject(self, post_id: int, reason: str) -> None:
        self._require(post_id, {"needs_review", "approved", "scheduled"})
        self.store.set_status(post_id, "rejected", note=reason)

    def _require(self, post_id: int, states: set[str]) -> None:
        row = self.store.get_post(post_id)
        if row is None or row["status"] not in states:
            raise ValueError(f"post {post_id} is not in state {sorted(states)}")

    # --- scheduling & publishing ----------------------------------------------------------
    def schedule(self, start: datetime | None = None, days: int = 14) -> list[tuple[int, str]]:
        tz = ZoneInfo(self.config.schedule.timezone)
        start = (start or datetime.now(tz)).astimezone(tz)
        taken = self.store.taken_slots()
        slots = []
        for d in range(days):
            day = (start + timedelta(days=d)).date()
            for hhmm in self.config.schedule.slots:
                h, m = map(int, hhmm.split(":"))
                at = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                iso = at.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
                if at > start and iso not in taken:
                    slots.append(iso)
        done = []
        for row, slot in zip(self.store.posts("approved"), slots):
            self.store.set_status(row["id"], "scheduled", scheduled_at=slot)
            done.append((row["id"], slot))
        return done

    def publish_due(self, at: datetime | None = None) -> list[int]:
        at_iso = (at or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        publisher = get_publisher(self.config)
        published = []
        for row in self.store.due_posts(at_iso):
            package = json.loads(row["package"])
            results, failed = {}, False
            for platform in self.config.enabled_platforms:
                try:
                    results[platform] = publisher.publish(row["id"], platform,
                                                          build_payload(self.config, package, platform))
                except Exception as e:  # one platform failing must not block the others
                    log.exception("publish failed post=%s platform=%s", row["id"], platform)
                    results[platform] = {"error": str(e)}
                    failed = True
            self.store.set_status(row["id"], "failed" if failed else "published",
                                  published_at=now(), publish_result=json.dumps(results))
            if not failed:
                published.append(row["id"])
        return published

    # --- analytics ----------------------------------------------------------------------
    def import_metrics(self, csv_path: str | Path) -> int:
        """CSV columns: post_id, platform, views, likes, comments, shares, saves, follows"""
        n = 0
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                post_id, platform = int(row.pop("post_id")), row.pop("platform")
                self.store.upsert_metrics(post_id, platform, **{k: int(v or 0) for k, v in row.items()})
                n += 1
        return n

    # --- the whole day ------------------------------------------------------------------
    def run_daily(self) -> dict:
        created = self.create_posts()
        scheduled = self.schedule()
        published = self.publish_due()
        return {"created": created, "scheduled": scheduled, "published": published}
