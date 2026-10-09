# Gita Studio

An automated content studio for the Bhagavad Gita, running on Claude (`claude-opus-5-5`). It takes care of the whole loop: picking verses and ideas, writing scripts, checking them against the scripture, art direction for AI visuals, captions for each platform, scheduling, publishing, and learning from analytics.

```
 analytics ──► strategy brief ──► ideas ──► script ──► scripture review ──┬─► rejected (logged)
     ▲                                          ▲             │ revise    │
     │                                          └─────────────┘ (≤2x)     ▼
     │                                                     visual prompts + captions
     │                                                                    │
     └──── publish ◄── schedule ◄── [human approval, on by default] ◄─────┘
```

## Why it is built this way

Realistic AI content about a sacred text can go wrong in two ways: **misquoting scripture** and **disrespectful depiction**. Both are covered by design:

| Risk | Safeguard |
|---|---|
| Claude "remembering" a verse wrong, or inventing a fake Krishna quote | Verse text comes **only** from `data/verses.json`. Ideas that point at verses outside the corpus are dropped. The quoted verse is checked word for word in code, not only by the model. |
| A modern spin presented as scripture | A separate reviewer call scores fidelity and respect (0–10) against the source verse and traditional commentary. If it asks for revisions, the script gets up to 2 rewrites. Anything below `min_fidelity_score` is rejected before any money is spent on visuals. |
| Offensive or sexualised depiction of deities | `depiction_rules` in `config.yaml` go into every script, reviewer and art-direction prompt, and into the negative prompts. |
| Platform penalties for unlabelled synthetic media | Every caption ends with an AI-disclosure line. Every publish payload carries `ai_generated: true` so your poster sets YouTube's, Meta's and TikTok's AI-content labels. |
| Fully automated mistakes at scale | `require_human_approval: true` by default. Set it to `false` only after you trust the reviewer's output. |

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env               # add ANTHROPIC_API_KEY (or run `ant auth login`)

# 1. Load a translation you have the rights to use (CSV or JSON)
#    CSV columns: chapter,verse,sanskrit,transliteration,translation[,source]
gita import-verses my_gita.csv --source "Translator name, edition, licence"
#    Or try it out with the 2-verse sample:
cp data/verses.sample.json data/verses.json

# 2. Make content
gita create -n 3                    # ideas → scripts → review → visuals → captions
gita queue                          # see what's waiting for you
gita show 1                         # full package: script, review, prompts, captions
gita approve 1 2                    # or: gita reject 3 "tone too casual"

# 3. Ship it
gita schedule                       # put approved posts into your posting slots
gita publish                        # publish everything that's due (default: dry run to ./outbox)

# 4. Learn
gita import-metrics metrics.csv     # post_id,platform,views,likes,comments,shares,saves,follows
gita strategy                       # Claude writes next week's brief; the ideas stage reads it
```

`gita run-daily` runs create, schedule and publish in one go. That is the command the scheduler calls.

## Full automation (GitHub Actions)

`.github/workflows/studio.yml` runs the studio on a schedule:

| When | What |
|---|---|
| Daily, 05:25 Nepal time | `run-daily`: new posts go into the review queue, approved posts get scheduled |
| Every 30 min | `publish`: posts any scheduled post that is due |
| Mondays | `strategy`: refreshes the brief from analytics |
| Manual (Actions tab → Run workflow) | `approve 12 13`, `reject 14 reason`, `queue`, `status`, … |

State (`studio.db`, media prompts, outbox) is saved to a `studio-state` branch between runs. Add these repository secrets: `ANTHROPIC_API_KEY`, plus `FAL_KEY` when `media.provider` is `fal`, `MEDIA_WEBHOOK_URL`, `PUBLISH_WEBHOOK_URL` and `WEBHOOK_SECRET` once you switch those providers to `webhook`. Commit your `data/verses.json` to the repo, or to the state branch, so the runner can read it.

## Visuals and posting

Two connection points, both set in `config.yaml`:

- **`media.provider`**
  - `prompt_only` (default): writes `media/post-NNNNN/prompts.md` with a shared style guide plus one image/video/voice/music prompt per scene. Paste these into any generator.
  - `webhook`: POSTs them to an n8n/Make/Zapier flow (or your own service) that calls your generators and returns asset URLs.
  - `fal`: renders each image asset with Nano Banana 2 and each video asset as a still animated by Veo 3.1 Fast (silent, 4/6/8 s), on fal.ai. Files land in `media/post-NNNNN/`. `media.fal.daily_budget_usd` is a hard daily stop tracked in `media/spend.json`; assets past it are skipped. Needs the `FAL_KEY` secret.
- **`publishing.provider`**
  - `outbox` (default): a safe dry run that writes one JSON per platform.
  - `webhook`: POSTs to Buffer/Make/Zapier/n8n or your own poster. Those tools hold the platform OAuth tokens, which keeps them out of this repo. Payloads are HMAC-signed when `WEBHOOK_SECRET` is set.

## Layout

```
gita_studio/
  scripture.py   verse corpus, chapter metadata (700 verses), importer
  prompts.py     every system prompt + the scripture guardrails (edit tone here)
  models.py      typed outputs for every stage (structured outputs)
  llm.py         Claude wrapper: schema-validated output, effort per stage, refusal fallback
  stages.py      strategy, ideas, script, review, visuals, captions
  pipeline.py    orchestration, review gate, scheduler, publisher, metrics
  store.py       SQLite
  integrations/  media + publishing backends (prompt_only/outbox/webhook)
config.yaml      brand voice, depiction rules, platforms, slots, thresholds
```

## Notes

- **Translation rights.** Many popular English translations are under copyright. Use a public-domain translation, one you have a licence for, or your own. The `source` field appears on every post.
- **Chapter 13** has 34 verses in the standard 700-verse recension. Editions that include the extra opening verse (35) will fail import validation for 13.35; adjust `CHAPTERS` in `scripture.py` if that is your edition.
- **Cost control.** Effort is set per stage in `config.yaml` (`model.effort`). Captions run at `medium`; review stays at `high`.
- Tests run offline with a fake model: `pytest`.
