---
name: run-gita-studio
description: Build, run, test and drive Gita Studio (the `gita` posts CLI and the `gita-reels` reel maker and its web studio). Use when asked to run, start or screenshot the reels studio, make a test reel or animatic, try the gita or gita-reels CLI, run the tests, or check a change works end to end without API keys or spend.
---

Gita Studio is two Python CLIs from one package: `gita-reels` (plans, generates, edits and validates vertical reels, plus a
local web studio on :8765) and `gita` (text/caption posts pipeline). Agents drive it with two committed scripts, both
needing **no API keys and no spend**: `smoke.sh` for the CLIs and `driver.mjs` (Playwright, headless Chromium) for the web
studio. Both use the `template` planner and `animatic` provider, which render labelled placeholder clips locally with ffmpeg.

All paths are relative to the repo root.

## Prerequisites

Python 3.10+ (verified on 3.13), `ffmpeg`/`ffprobe` on PATH, Node with Playwright for the web driver (both were
preinstalled in the cloud container; nothing needed `apt-get`).

## Build

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,reels]"
```

## Run (agent path)

### CLIs: smoke script (~75 s)

```bash
. .venv/bin/activate
bash .claude/skills/run-gita-studio/smoke.sh e03
```

It plans episode `e03` with the template planner, makes one animatic take per shot, selects them, assembles, checks
`validation.ok` in `project.json`, grabs a frame to `reels/<id>/frame_4s.png` (look at it), then runs the `gita` posts
pipeline end to end (create, approve, schedule, publish to outbox) with the tests' `FakeClaude` in a temp dir.
Ends with `SMOKE OK`. Output folders land in `reels/` (git-ignored).

Single steps, if you need them (`gita-reels --help` lists the rest):

```bash
gita-reels providers                       # what is configured; spends nothing
gita-reels new --episode e01 --planner template --option provider=animatic --option width=540 --option height=960
gita-reels take reels/<id> 1 --provider animatic
gita-reels select reels/<id> 1 t01
gita-reels assemble reels/<id>
gita-reels show reels/<id>
```

### Web studio: Playwright driver (~70 s)

```bash
. .venv/bin/activate
(gita-reels studio > /tmp/studio.log 2>&1 &)
timeout 30 bash -c 'until curl -sf http://127.0.0.1:8765/api/overview >/dev/null; do sleep 1; done'
node .claude/skills/run-gita-studio/driver.mjs flow --episode e02
```

`flow` fills the New reel form (9:16, 1080p, animatic provider, template planner), clicks **Plan shots**, opens the new
project, clicks **Generate take** on every shot, **Approve** on every take, then **Assemble and export**, and waits for the
validated `final.mp4`. Screenshots go to `/tmp/gita-studio-shots/NN-<step>.png` (`form`, `planned`, `takes-ready`,
`approved`, `assembled`, or `failure`); set `SHOTS=<dir>` to move them. Exit code 1 if anything fails or the export fails
validation; page errors and `alert()`s are printed.

`node .claude/skills/run-gita-studio/driver.mjs ss --project <id>` takes one screenshot with that project open.

The studio must be started from the repo root: its jobs run `python -m gita_reels.cli` with the server's cwd and
interpreter. Job logs: `reels/.jobs/<job>.log`.

Stop it:

```bash
lsof -ti:8765 -sTCP:LISTEN | xargs -r kill
```

## Run (human path)

`gita-reels studio`, then open http://127.0.0.1:8765. Real footage needs `FAL_KEY`; Claude planning, review and visual
QC need `ANTHROPIC_API_KEY`. Neither was available here, so only the animatic/template path is verified.

## Test

```bash
pytest -q          # 34 passed, ~3 min; tests/test_pipeline.py + test_llm.py alone take ~1 s
```

## Gotchas

- **720p animatics fail validation.** At 720x1280 (the studio's "720p" option) the template plan's shot 3
  (`macro_insert`, `locked_off` move) renders a static placeholder and assembly ends `failed_validation` with
  "frozen picture: from 5.54s for 1.96s". 540x960 and 1080x1920 pass. It is deterministic, so `driver.mjs` uses 1080p.
  Real (fal) takes are unaffected.
- **`gita-reels assemble` exits 0 even when the export fails validation**; read `validation.ok` / `status` from
  `reels/<id>/project.json` (as `smoke.sh` does) rather than trusting the exit code.
- **`FakeClaude` only works with the sample corpus.** Its canned BG 2.47 quote matches `data/verses.sample.json`, not
  the Besant translation in `data/verses.json`; against the full corpus `create_posts` returns no posts.
- **`gita create` without `ANTHROPIC_API_KEY`** dies with `TypeError: "Could not resolve authentication method..."`.
  Use the `FakeClaude` path in `smoke.sh` instead. `gita status`/`gita queue` work keylessly but create `studio.db` in
  the repo root (git-ignored).
- **Piping `gita-reels` output into `head`** raises `BrokenPipeError` tracebacks; capture into a variable first.
- **ESM `import 'playwright'` fails** (`ERR_MODULE_NOT_FOUND`) because Node ignores `NODE_PATH` for ESM and Playwright
  lives in `/opt/node-tools/node_modules`. `driver.mjs` loads it with `createRequire`.
- **"Executable doesn't exist at ~/.cache/ms-playwright/..."** in a shell without `PLAYWRIGHT_BROWSERS_PATH`: the
  container's browsers are in `/opt/pw-browsers`. `driver.mjs` sets that default itself, before loading Playwright
  (setting it after `require` is too late).
- The studio polls jobs every 3 s, so a new project shows up in the list a few seconds after its plan job ends; the
  driver waits on `/api/projects` rather than the DOM. Every API write needs the `X-Studio: 1` header and a localhost
  Host (the page's own `fetch` handles that).
- Animatic frames carry an "ANIMATIC - placeholder" banner and the status `animatic`; they can never be published.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `status failed_validation`, frozen picture | Animatic at 720p; use 1080p or 540x960 (see Gotchas). |
| `ValueError: not enough values to unpack` in the posts step | Corpus is `data/verses.json`; use `data/verses.sample.json` with `FakeClaude`. |
| `ERR_MODULE_NOT_FOUND: playwright` | Run the committed `driver.mjs` (it uses `createRequire`), or set the path in its loader list. |
| Driver: "no new project after 60 s" | Read the newest `reels/.jobs/*.log`; the studio was probably started outside the repo root. |
