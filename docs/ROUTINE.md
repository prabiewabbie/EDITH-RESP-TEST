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

## 6. Report

End with one line: the episode, its status, the YouTube URL if published, and fal spend
(`reels/spend.json`).
