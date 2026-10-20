# Gita Reels: fully animated, editable reels on autopilot

`gita_reels/` makes 15–30 second vertical reels that tell the Bhagavad Gita in order, in the style
measured from the reference reel (`style/style_bible.json`): photoreal cinematic CGI, golden-hour
backlight, ~2 s shots, smooth camera, varied shot scale, music-led, almost no text.

Every second on screen is generated video. Stills exist only as hidden keyframes fed into the video model.

It runs two ways: **unattended** (`autopilot`, the daily GitHub job) and **shot by shot** in the local
web studio (`gita-reels studio`), where you edit each shot's cinematography, generate and compare takes,
approve the ones you like and assemble the reel.

## Quick start

```bash
pip install -e ".[reels]"            # plus ffmpeg on your PATH
export ANTHROPIC_API_KEY=...         # planning, review and visual QC
export FAL_KEY=...                   # video and keyframes (or see "Free paths" below)
gita-reels providers                 # what is configured and what is missing; spends nothing
gita-reels analyze my_reference.mp4 --apply   # learn the reference's shots, motion, light and palette
gita-reels studio                    # open http://127.0.0.1:8765
```

In the studio: pick an episode (or switch to **Concept** and describe your own scene, character and
environment), choose aspect ratio, duration, lens/move/lighting preferences and a provider, and press
**Plan shots**. Each shot card lets you change lens, camera move, lighting, focus, height, timing,
composition and the exact prompts; **Generate take** makes a new take without touching earlier ones;
hover a take to preview it, read its QC notes, then **Approve** or **Reject**. **Assemble and export**
edits the approved takes, exports and validates the file.

Without any key you can still try everything with the **animatic** provider and the template planner:
it moves a labelled placeholder card along each planned camera path. Animatic reels are clearly marked
on every frame and can never be published.

## Cinematography layer

`style/cinematography.yaml` holds the presets the planner chooses from and the composer expands:

- **Lenses:** portrait 85 mm, normal 50 mm, wide 32 mm, extreme wide 21 mm (once per reel at most),
  telephoto 120 mm, macro 100 mm, each with focal length, aperture and what it looks like.
- **Camera moves:** push-in, pull-back, lateral track, low-angle heroic reveal, crane, subtle orbit,
  locked-off, ground-level track, stabilised handheld, rack focus. Each has a start and end framing,
  speed, easing, what to keep and what to avoid (for example "no digital zoom").
- **Lighting:** golden backlight, golden side light, volumetric rays, dawn haze, restrained divine
  radiance, firelight.
- **Focus strategies, grades and quality constraints** (positive and negative, adapted per shot).

Each shot is a structured spec (`writer.Shot`): subject, environment, composition with depth layers,
camera height, lens, focus, move and its exact detail, subject motion, lighting, atmosphere, materials,
final frame, screen direction, timing, cut type and sound cues. `cinema.py` turns it into prompts in a
fixed order (subject, environment, composition, angle, lens, depth of field, camera move, subject
motion, light, atmosphere, materials, grade, temporal consistency, final frame, quality). Keyframe
prompts describe the move's first frame with no motion; image-to-video prompts lead with the camera move
and the subject motion, because the image already fixes the look. Each provider gets its own shape
(`style: veo | kling | generic`) and only the fields its schema supports.

**Honest limits:** a hosted video model interprets "85 mm, f/2, slow dolly in" probabilistically. Nothing
in this pipeline physically enforces a camera path; the visual QC checks afterwards whether the move
reads as a physical move rather than a zoom, and a take that doesn't is flagged.

## Reference analysis

`gita-reels analyze <video>` (or the studio's Reference panel) writes `analysis/<name>/`:

- `contact_sheet.jpg`: start, middle and end frame of every detected shot
- `report.md` / `report.json`: per-shot duration, measured camera motion (pan/tilt from phase
  correlation, push/pull from a scale search), motion energy, a depth-of-field cue (centre vs edge
  sharpness), brightest region and light cue, approximate subject position, exposure percentiles,
  saturation, warm share, palette, and loudness
- with `ANTHROPIC_API_KEY`: a shot-by-shot cinematography breakdown from Claude (shot type, angle,
  lens estimate, lighting setup, why it works), plus a style paragraph and shot pattern

Lens, aperture and camera body cannot be recovered from rendered pixels; the report labels them as
estimates. `--apply` merges the measurements into `style/style_bible.json`, which every plan follows.
Keep the reference video itself out of the repo (`analysis/uploads/` is git-ignored) unless you own it.

## Free paths

- **Your own clips:** make a shot in any tool you like (free tiers included) and use **Your clip** on
  the shot card, or `gita-reels import-take <project> <shot> clip.mp4`. It becomes a take like any other.
- **Your own keyframe:** **Your keyframe** (or `gita-reels keyframe`) sets the starting image for that
  shot's next takes, so no image model is needed.
- **A model on your own GPU:** set `local.video_command` (and optionally `local.image_command`) in
  `reels.yaml` to any command, such as an open-weight image-to-video model or a ComfyUI script. It
  receives the keyframe, the composed prompt files, the duration and the output path, and its clip is
  checked like any other take. Choose the **local** provider.
- **Planning without an API key:** `gita-reels brief --episode e04` prints the planner's full prompt
  and the JSON format. Paste it into a Claude chat, save the reply as `plan.json`, then
  `gita-reels new --plan-file plan.json`. Imported and template plans are never reviewed by the
  pipeline, so the autopilot never publishes them; you review them yourself.

## Pipeline

1. **Storyline:** `style/episodes.json` has 26 episodes from Dhritarashtra's question (1.1) to Sanjaya's
   last words (18.78). The autopilot takes the next one each day.
2. **Plan:** Claude writes a 6–12 shot list to the style guide. Code then enforces the length (15–30 s),
   at most one sideways widescreen shot, and a verse line only if it's a word-for-word excerpt of `data/verses.json`.
3. **Review:** a separate Claude call checks scripture fidelity and respect. If the review fails, the
   reel is dropped before any video is paid for.
4. **Characters:** one reference sheet per character (`style/refs/`) is made once and passed into
   every keyframe, so Krishna and Arjuna look the same across shots and reels.
5. **Takes:** each shot gets a keyframe, which Claude checks, then image-to-video, whose frames from
   across the whole clip Claude checks again for identity, anatomy, artifacts, respect, match to the
   spec, camera motion and lighting continuity. A cheap measurement also flags clips that barely move
   or flicker. Every attempt is kept as its own take (`shots/NN/tKK/`); the autopilot retries a
   failing shot up to 2 times with a fix hint. If the first video model fails, the next one is tried.
6. **Edit:** the selected takes are trimmed and conformed (1080×1920 at 24 fps by default, any size per
   project), nudged toward a common colour balance, then graded with the preset in `reel.grade`.
   Cuts, match cuts and occasional flashes; the verse title, channel handle and "AI-generated" label;
   music, the model's own ambience, sound effects matched from `sfx/` to each shot's cues, and an
   optional voice-over, mixed to −14 LUFS with a limiter. H.264 by default, H.265 optional.
7. **Validate:** the export is checked for size, frame rate, duration, pixel format, streams, black or
   frozen frames, audio clipping and loudness (`validation` in `project.json`). A failing file is
   never published.
8. **Publish:** the reel is uploaded to YouTube with the synthetic-media label, using `publish.privacy`.

## Cost controls

- Each run prints an estimate, and `gita-reels plan` shows it without spending on video.
- `reel_budget_usd` (default $8) and `daily_budget_usd` (default $15) are hard stops.
- Every fal result is cached by its exact input, so re-running a reel doesn't pay twice.

The video chain in `reels.yaml` is ordered cheapest first; a model is only used when the one
before it fails:

| Model | Price (fal, Oct 2026) | Role |
|---|---|---|
| Seedance 1.5 Pro, 720p, no audio | about $0.025/s (token-billed) | default |
| Wan Pro, 1080p | $0.16 per clip | fallback |
| Veo 3.1 Fast, no audio | $0.10/s | premium fallback |

A 15–20 second reel is 6–8 shots. With Seedance that is about $0.10 of video per shot plus
about $0.08 per keyframe, so **about $1–1.50 per reel, $2 with retakes**. Keyframes are now
the larger half. Prices change: run `gita-reels check-models` and correct `price_per_second`
or `price_per_video`, since budgets are enforced from those numbers.

Claude costs nothing extra in split mode (below): planning, scripture review and visual QC
run in a scheduled Claude Code task on your plan instead of the paid API.

## The editable project (one folder per reel)

| Path | What it is |
|---|---|
| `shots/NN/tKK/` | every take: keyframe, raw clip, QC frames, `take.json` (prompts, request, QC, metrics, cost) |
| `clips/shot_NN.mp4` | conformed shots exactly as used in the edit |
| `titles/verse.png`, `titles/brand.png` | transparent text layers you can replace |
| `audio/music.wav`, `ambience.wav`, `sfx.wav`, `voice.wav`, `mix.wav` | separate audio stems |
| `captions.srt` | verse line and narration with timings |
| `timeline.xml` | FCP 7 XML. In Premiere Pro: File > Import. In DaVinci Resolve: File > Import > Timeline |
| `timeline.otio` | OpenTimelineIO, for Resolve and other OTIO tools |
| `final.mp4` | the flattened master that was published |
| `project.json` | plan, review, selected takes, validation, cost |

After copying a reel folder to another computer, run `gita-reels timeline <folder>` to rewrite the
timelines with that machine's file paths.

## Commands

```bash
gita-reels studio                  # the shot-by-shot web studio
gita-reels providers               # provider config and credentials, no spend
gita-reels analyze ref.mp4 --apply # reference breakdown + contact sheet; update the style bible
gita-reels new --episode e04       # (or --concept "...") plan a project to work on shot by shot
gita-reels take <project> 3 --provider animatic   # one new take of shot 3
gita-reels select <project> 3 t02  # approve a take (--force overrules a failed QC)
gita-reels assemble <project>      # edit, export, validate
gita-reels validate final.mp4      # check any exported file
gita-reels check-models            # compare fal model fields with fal's live schema
gita-reels episodes                # storyline and the next episode
gita-reels plan --episode e04      # shot list + review + cost estimate; no video spend
gita-reels make --episode e04      # full reel into reels/<time>-e04/
gita-reels publish reels/<folder>  # upload one reel
gita-reels autopilot               # next episode: make, then publish if auto_publish is on
gita-reels youtube-auth            # one-time; save the printed JSON as the YOUTUBE_TOKEN_JSON secret
```

## Automation

Two ways to run the channel unattended:

**Split mode (default, no Anthropic API key).** A scheduled Claude Code task in the project
does every Claude step on your plan; GitHub Actions does the media. The task follows
[`docs/ROUTINE.md`](ROUTINE.md):

1. The task restores the `reels-state` branch, runs `gita-reels brief` for the next episode,
   writes the plan and an independent fidelity/respect/craft review to `queue/eNN.json`, and
   pushes it.
2. It dispatches `reels.yml` with `command: produce`. Actions makes one take per shot and a
   `review.jpg` sheet for each, and saves the take records and sheets to `reels-state`
   (clips stay in the Actions cache for 10 days).
3. The task looks at each sheet and records `gita-reels qc <folder> <shot> <take> pass|fail
   --note "…"`. A failed note becomes the retake prompt's "Fix: …" line.
4. It dispatches `command: finish`. Actions retakes failed shots (back to step 3), or, when
   every shot passed, assembles, validates and publishes, then moves the storyline on.

A plan without an approving review is never produced, and an animatic is never published.

**Autopilot (paid Anthropic API).** Set the repository variable `REELS_MODE` to `api` and
`reels.yml` runs `autopilot` daily, doing everything in one job. Otherwise the daily schedule
leaves the work to the scheduled task.

Either way the workflow uploads the editable project as an artifact (kept 30 days) and keeps
the storyline position, spend ledger, character sheets, queue and fal cache on `reels-state`.
Secrets: `FAL_KEY` and `YOUTUBE_TOKEN_JSON` (plus `ANTHROPIC_API_KEY` for autopilot only).
Without `FAL_KEY` it skips quietly.

## Troubleshooting

- **`gita-reels providers` says FAL_KEY or ANTHROPIC_API_KEY is not set:** export them in the shell
  that runs the studio, or use the free paths above.
- **A fal model rejects a field:** run `gita-reels check-models`; it compares the configured fields
  with fal's published schema. Fix `reels.yaml` (`duration_format`, `negative_prompt`, `extra`).
- **Takes keep failing QC:** read the notes on the take card, adjust the shot spec (often a simpler
  move or a closer lens), or approve with **Use anyway** if you disagree with the reviewer.
- **Validation fails:** the reasons are listed under the preview; the reel is kept but not published.
- **Premiere opens the timeline as 16:9:** import `timeline.xml` rather than the OTIO file.
