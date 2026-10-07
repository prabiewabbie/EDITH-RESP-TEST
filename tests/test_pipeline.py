import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gita_studio import models as m
from gita_studio.config import load_config
from gita_studio.pipeline import Studio
from gita_studio.scripture import TOTAL_VERSES, Corpus, import_verses, validate_ref
from gita_studio.store import Store

ROOT = Path(__file__).resolve().parents[1]
QUOTE = "Your right is to the action alone, never to its fruits. Do not let the fruit of action be your motive, and do not cling to inaction."


class FakeClaude:
    """Returns canned stage outputs; records which stages ran."""

    def __init__(self, reviews=None, quote=QUOTE):
        self.calls = []
        self.reviews = list(reviews or [])
        self.quote = quote

    def structured(self, stage, system, prompt, schema, max_tokens=16000):
        self.calls.append(stage)
        if schema is m.IdeaBatch:
            return m.IdeaBatch(ideas=[
                m.Idea(chapter=2, verse=47, format="short_video", angle="exam results anxiety",
                       hook="You studied. Now let go.", rationale="Action without attachment to results"),
                m.Idea(chapter=99, verse=1, format="carousel", angle="hallucinated", hook="x", rationale="x"),
            ])
        if schema is m.Script:
            return m.Script(title="Let go of the result", hook="You studied. Now let go.",
                            scenes=[m.Scene(seconds=5, narration="n", on_screen_text="t", visual="v")],
                            verse_quote=f"“{self.quote}”", reflection="r", call_to_action="Save this")
        if schema is m.Review:
            if self.reviews:
                return self.reviews.pop(0)
            return m.Review(fidelity_score=9, respect_score=10, quote_is_exact=True, issues=[], verdict="approve")
        if schema is m.VisualPlan:
            return m.VisualPlan(style_guide="warm dawn light", assets=[
                m.VisualAsset(kind="video", scene_index=0, prompt="Arjuna at dawn", aspect="9:16")])
        if schema is m.Distribution:
            return m.Distribution(copies=[m.PlatformCopy(platform="instagram", language="en", caption="BG 2.47 ...",
                                                         hashtags=["#gita"], alt_text="a")])
        raise AssertionError(schema)


@pytest.fixture
def studio_factory(tmp_path):
    def make(claude, human=True):
        config = load_config(ROOT / "config.yaml")
        config.paths.db = str(tmp_path / "t.db")
        config.paths.outbox = str(tmp_path / "outbox")
        config.paths.media = str(tmp_path / "media")
        config.review.require_human_approval = human
        corpus = Corpus.load(ROOT / "data" / "verses.sample.json")
        return Studio(config, claude=claude, store=Store(config.paths.db), corpus=corpus)
    return make


def test_full_flow_with_human_gate(studio_factory, tmp_path):
    studio = studio_factory(FakeClaude())
    ids = studio.create_posts(2)
    assert len(ids) == 1, "idea pointing at a verse outside the corpus must be dropped"
    post = studio.store.get_post(ids[0])
    assert post["status"] == "needs_review"

    pkg = json.loads(post["package"])
    assert all("AI-generated" in c["caption"] for c in pkg["distribution"]["copies"])
    assert (tmp_path / "media" / f"post-{ids[0]:05d}" / "prompts.md").exists()

    assert studio.schedule() == [], "unapproved posts must never be scheduled"
    studio.approve(ids[0])
    [(pid, slot)] = studio.schedule()
    assert pid == ids[0]

    later = datetime.fromisoformat(slot) + timedelta(minutes=1)
    assert studio.publish_due(at=later) == [pid]
    assert studio.store.get_post(pid)["status"] == "published"
    out = json.loads((tmp_path / "outbox" / f"post-{pid:05d}" / "instagram.json").read_text())
    assert out["ai_generated"] is True and out["verse_ref"] == "BG 2.47"


def test_auto_approve_mode(studio_factory):
    studio = studio_factory(FakeClaude(), human=False)
    [pid] = studio.create_posts(1)
    assert studio.store.get_post(pid)["status"] == "approved"


def test_revision_loop_then_approve(studio_factory):
    bad = m.Review(fidelity_score=5, respect_score=9, quote_is_exact=True, verdict="revise",
                   issues=[m.Issue(severity="major", where="reflection", problem="overclaims", fix="soften")])
    claude = FakeClaude(reviews=[bad])
    studio = studio_factory(claude)
    [pid] = studio.create_posts(1)
    assert claude.calls.count("script") == 2
    assert studio.store.get_post(pid)["status"] == "needs_review"


def test_misquoted_verse_is_rejected(studio_factory):
    claude = FakeClaude(quote="Do your duty and success will surely follow.")
    studio = studio_factory(claude)
    [pid] = studio.create_posts(1)
    post = studio.store.get_post(pid)
    assert post["status"] == "rejected"
    assert "visuals" not in claude.calls  # no spend on a post that can't ship


def test_reject_blocks_publishing(studio_factory):
    studio = studio_factory(FakeClaude(), human=False)
    [pid] = studio.create_posts(1)
    studio.reject(pid, "tone")
    assert studio.schedule() == []


def test_scripture_reference_data():
    assert TOTAL_VERSES == 700
    assert validate_ref(18, 78) and not validate_ref(18, 79) and not validate_ref(19, 1)


def test_import_verses_csv(tmp_path):
    src = tmp_path / "v.csv"
    src.write_text("chapter,verse,sanskrit,transliteration,translation\n2,14,s,t,tr\n", encoding="utf-8")
    dest = tmp_path / "verses.json"
    shutil.copy(ROOT / "data" / "verses.sample.json", dest)
    assert import_verses(src, dest, source="Test") == 1
    assert len(Corpus.load(dest)) == 3
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.csv"
        bad.write_text("chapter,verse,sanskrit,transliteration,translation\n2,99,s,t,tr\n", encoding="utf-8")
        import_verses(bad, dest, source="Test")
