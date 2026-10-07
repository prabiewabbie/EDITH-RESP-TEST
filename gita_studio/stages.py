"""The content stages. Each one is a single typed Claude call with code-controlled inputs."""
from __future__ import annotations

import json
import random

from . import prompts
from .config import Config
from .llm import Claude
from .models import Distribution, Idea, IdeaBatch, Review, Script, StrategyBrief, VisualPlan
from .scripture import CHAPTERS, Corpus, Verse


def plan_strategy(claude: Claude, config: Config, performance: list[dict]) -> StrategyBrief:
    prompt = (
        "Post-level performance (most recent first, summed across platforms):\n"
        f"{json.dumps(performance, ensure_ascii=False, indent=1)}\n\n"
        "Write next week's strategy brief."
    )
    return claude.structured("strategy", prompts.strategist(config), prompt, StrategyBrief)


def generate_ideas(
    claude: Claude,
    config: Config,
    corpus: Corpus,
    count: int,
    used: list[tuple[int, int]],
    strategy: dict | None,
) -> list[Idea]:
    used_set = set(used)
    available = [v for v in corpus.all() if (v.chapter, v.verse) not in used_set] or corpus.all()
    if config.content.verse_selection == "sequential":
        candidates = available[: max(count * 3, count)]
    else:
        # Give Claude a varied shortlist rather than the full corpus to keep requests small.
        candidates = random.sample(available, k=min(len(available), max(count * 8, 12)))

    verse_list = "\n".join(f"- {v.ref} ({CHAPTERS[v.chapter][0]}): {v.translation}" for v in candidates)
    prompt = (
        f"Generate exactly {count} post ideas. Allowed formats: {', '.join(config.content.formats)}.\n"
        f"Choose ONLY from these verses:\n{verse_list}\n\n"
        f"Recently used (avoid): {', '.join(f'{c}.{v}' for c, v in used[-40:]) or 'none'}\n"
        f"Current strategy brief: {json.dumps(strategy) if strategy else 'none yet - favour variety'}"
    )
    batch = claude.structured("ideate", prompts.ideator(config), prompt, IdeaBatch)
    allowed = {(v.chapter, v.verse) for v in candidates}
    # Drop anything that points at a verse we didn't supply: we only ever publish corpus text.
    return [i for i in batch.ideas if (i.chapter, i.verse) in allowed and i.format in config.content.formats][:count]


def _verse_block(verse: Verse) -> str:
    return (
        f"Reference: {verse.ref} — Chapter {verse.chapter}, {CHAPTERS[verse.chapter][0]}\n"
        f"Sanskrit:\n{verse.sanskrit}\n"
        f"Transliteration:\n{verse.transliteration}\n"
        f"Translation (quote exactly):\n{verse.translation}\n"
        f"Translation source: {verse.source}"
    )


def write_script(claude: Claude, config: Config, idea: Idea, verse: Verse, feedback: Review | None = None) -> Script:
    prompt = (
        f"{_verse_block(verse)}\n\n"
        f"Format: {idea.format}\nAngle: {idea.angle}\nProposed hook: {idea.hook}\nRationale: {idea.rationale}"
    )
    if feedback:
        prompt += (
            "\n\nA reviewer rejected the previous draft. Fix every issue:\n"
            + "\n".join(f"- [{i.severity}] {i.where}: {i.problem} -> {i.fix}" for i in feedback.issues)
        )
    return claude.structured("script", prompts.scriptwriter(config), prompt, Script)


def review_script(claude: Claude, config: Config, script: Script, verse: Verse) -> Review:
    prompt = f"{_verse_block(verse)}\n\nScript to review:\n{script.model_dump_json(indent=1)}"
    review = claude.structured("review", prompts.reviewer(config), prompt, Review)
    # Belt and braces: a deterministic check on the one thing that must never drift.
    if _normalise(script.verse_quote) != _normalise(verse.translation):
        review.quote_is_exact = False
        review.verdict = "revise" if review.verdict == "approve" else review.verdict
    return review


def _normalise(text: str) -> str:
    return " ".join(text.strip().strip("\"'“”‘’").split())


def plan_visuals(claude: Claude, config: Config, idea: Idea, script: Script) -> VisualPlan:
    aspects = sorted({p.aspect for p in config.enabled_platforms.values()})
    prompt = (
        f"Format: {idea.format}\nTarget aspect ratios: {', '.join(aspects)} (use the first for the master asset)\n"
        f"Script:\n{script.model_dump_json(indent=1)}"
    )
    return claude.structured("visuals", prompts.art_director(config), prompt, VisualPlan)


def write_distribution(claude: Claude, config: Config, verse: Verse, script: Script) -> Distribution:
    disclosure = config.content.ai_disclosure.format(translation_source=verse.source)
    prompt = (
        f"Platforms: {', '.join(config.enabled_platforms)}\nLanguages: {', '.join(config.brand.languages)}\n"
        f"Verse reference: {verse.ref}\nAI disclosure line (must end every caption): {disclosure}\n\n"
        f"Script:\n{script.model_dump_json(indent=1)}"
    )
    dist = claude.structured("captions", prompts.distributor(config), prompt, Distribution)
    for copy in dist.copies:
        if disclosure not in copy.caption:
            copy.caption = f"{copy.caption.rstrip()}\n\n{disclosure}"
    return dist
