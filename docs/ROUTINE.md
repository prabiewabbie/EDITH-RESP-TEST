# Scheduled task runbook (split mode)

This is what the scheduled Claude Code task does on each run. It replaces the Anthropic API:
you plan, review the scripture and judge the footage; GitHub Actions spends on fal and edits.
Never print or commit secrets. Work in repository `prabiewabbie/EDITH-RESP-TEST`.

## 0. Set up

```bash
pip install -e ".[reels]"
git fetch origin reels-state && git checkout origin/reels-state -- . && git reset -q
```

`reels-state` holds `reels_state.json` (storyline position), `queue/`, and for each reel its
`project.json`, every take's `take.json` and `review.jpg`. It does not hold video.

## 1. Is a reel already in review?

`gita-reels show` on the newest `reels/*/` folder. If its status is `awaiting_qc`, go to step 4.
If it is `in_production`, an Actions run is still working: check the latest `reels.yml` run and
stop if it is in progress.

## 2. Plan the next episode

```bash
gita-reels episodes           # the arrow marks the next episode, e.g. e07
gita-reels brief --episode e07 > brief.txt
```

Read `brief.txt` and answer it yourself, exactly as the request says: a shot plan that matches
the first schema, using only the lens, move, lighting and grade ids it lists.

Then review the plan as a separate, sceptical pass (second schema): is it faithful to the
chapter and verse, respectful (no comic or sexualised depiction of Krishna, Arjuna or any
deity, no violence beyond what the text describes), and well shot (lens variety, screen
direction kept, one ultrawide at most)? Give `verdict: "revise"` with issues if not, and
fix the plan before going on. The verse line must be copied verbatim from `data/verses.json`.

Write `{"plan": …, "review": …}` to `queue/e07.json`, check it loads with
`gita-reels new --episode e07 --plan-file queue/e07.json --planner template` in a scratch
copy, then push `queue/` to `reels-state` (commit on top of `origin/reels-state`).

## 3. Produce

Dispatch the `reels.yml` workflow on the default branch with `command: produce` (GitHub MCP
`actions_run_trigger`). Wait for it to finish, then restore `reels-state` again.

## 4. Review the takes

```bash
gita-reels show reels/<folder>
```

For each shot whose newest take is `ready` and not reviewed, open
`reels/<folder>/shots/NN/tKK/review.jpg` (keyframe, then frames across the clip) and judge:
identity matches the character sheet, anatomy (hands, faces), artifacts and flicker, respect,
match to the shot spec, camera move as specified, lighting consistent with neighbouring shots.

```bash
gita-reels qc reels/<folder> 3 t01 pass
gita-reels qc reels/<folder> 4 t01 fail --note "Arjuna's bow merges into his hand" --note "camera drifts right, should push in"
```

Notes must say what to change, because they become the retake prompt. Push the changed
`take.json` files to `reels-state`.

## 5. Finish

Dispatch `reels.yml` with `command: finish`. If shots were retaken, the run ends with
`awaiting_qc`: go back to step 4 on the next run. After `fal.max_retries_per_shot` failed
retakes the episode is abandoned and the storyline moves on. When every shot passed, the run
assembles, validates and (if `publish.auto_publish`) publishes, and advances the storyline.

## Theme reels (standalone, outside the storyline)

Plan by `style/master_prompt.md` and write `queue/theme-<slug>.json`:

```json
{"theme": {"verse": "2.47", "title": "...", "brief": "...", "series": "Battle POV", "format": "POV",
           "characters": {"arjuna": "<the Part 5 anchor, word for word>"}},
 "plan": {...}, "review": {...},
 "prompt_overrides": {"0": {"keyframe": "...", "video": "...", "negative": "..."}},
 "caption": "hook line\ncontext\n...", "hold": true}
```

`produce` takes a queued theme reel before the next episode and moves its file to `queue/done/`.
A theme reel never moves the storyline, and it is held (`hold`, default true): `finish` assembles and
stops at `ready`. Nothing uploads until a person says go; then dispatch `reels.yml` with
`command: release` and `privacy: private` (or whatever they chose).

Plan fields beyond the basics: per-shot `source_in` (where in the clip the shot starts), `source_of`
(reuse another shot's take, e.g. the closing wide of a burst), `ramp_at` / `ramp_frames` /
`ramp_speed` (speed ramp on the impact), `black_after`, `model` (e.g. Seedance 2.0, listed in
`reels.yaml` with `chain: false`), cut types `cut_on_action`, `whip_pan`, `smash_cut`, `j_cut`,
`l_cut`; reel-level `hook_text`, `question_text`, `verse_start` / `verse_end`, and `cue_sheet`. A cue
sheet makes `finish` order a music bed written to its sections (`fal.music_model`), then shape it so
the level follows each beat, the planned silence drops out, and designed hits and risers land on the
exact seconds listed. Hand-written prompts in `prompt_overrides` are sent as written (put the anchor,
the sound list and an "Avoid:" sentence in them yourself); retake notes are appended.

After seeing takes, set cut points without new spend:
`gita-reels trim reels/<folder> 5 --in 1.4 --ramp 0.3`, then push `project.json` to `reels-state`.

Every assembled reel gets `review/contact_sheet.jpg` (a frame every 0.5 s), `review/loudness.png`
(loudness against the beat map) and `review/audio.json` (LUFS, true peak, loudness per beat, silence
depth). Look at all three before asking for the go-ahead.

## 6. Report

End with one line: the episode, its status, the YouTube URL if published, and fal spend
(`reels/spend.json`).
