# Brief for Claude Code: fully animated Bhagavad Gita Shorts

Paste everything below the line into Claude Code, run from a clone of
`https://github.com/prabiewabbie/EDITH-RESP-TEST` (branch `claude/awesome-newton-2dhf2u`).

---

## Goal

Extend this repo so one command produces a 35–60 second vertical (1080×1920) **fully animated**
Short about one Bhagavad Gita verse, at the production quality of my reference video
(https://youtu.be/FcNOF6pmkR4). Every second on screen must be moving footage. No still images
with a zoom, no slideshow. Stills may exist only as hidden keyframes fed into the video model.

Build on what already exists. Don't start a new project:
- `youtube_shorts.py`: script writing, the scripture review, the cinematic brief, voice-over, ffmpeg edit, YouTube upload.
- `gita_studio/integrations/media.py`: `FalMedia`, which calls fal.ai with a daily `SpendLedger`.
- `config.yaml` → `media.fal`: model IDs, prices, `daily_budget_usd`.

Work in phases. Stop at every **CHECKPOINT** and show me the result before continuing.

## Phase 0: Learn the reference style (CHECKPOINT)

You can't watch YouTube, and don't download the video. I'll put 8–12 screenshots from different
moments in `reference/` and fill in the notes below. If the notes are empty, ask me these
questions one at a time.

```
REFERENCE NOTES (I fill this in)
Length:                         e.g. 50s
Average shot length:            e.g. 1.5-3s, fast cuts / slow
Camera:                         e.g. sweeping drone, orbit, slow push, handheld
Look:                           e.g. hyper-real 3D, painterly, anime, photoreal; palette
Characters:                     who appears, how they're styled
Transitions:                    hard cuts, whip pans, light flashes, match cuts
Text on screen:                 font style, size, word-by-word or full lines, position
Voice:                          deep male / female / none; pace; language
Music & sound:                  epic drums, choir, drone, sitar; whooshes, impacts
Structure:                      how it opens (first 2s), builds, and ends
What I like most:
```

From the screenshots, measure what you can: dominant colours as hex, contrast, grain, framing,
text position. Then write `style/style_bible.json` with these fields:
`look, palette_hex[], lighting, lens, camera_moves[], avg_shot_seconds, transitions[], text_style,
voice_style, music_style, sfx_style, structure_beats[], negative_prompt`.
Also write a one-paragraph `style_prompt` that gets prepended to every video prompt.
**CHECKPOINT:** show me the style bible and wait for my approval.

## Phase 1: Characters that look the same in every shot (CHECKPOINT)

AI video drifts between shots unless every shot starts from the same reference. Create
`style/characters.json` and generate one reference sheet per character with the image model in
`config.yaml`: front, three-quarter and profile views, neutral background, using the style bible.
- **Krishna:** traditional iconography. Blue-hued skin, peacock-feather crown, yellow pitambara,
  flute or charioteer's reins, serene expression. Dignified at all times.
- **Arjuna:** warrior prince, Gandiva bow, armour, conflicted then resolved.
- **Optional modern protagonist** for the "today" story thread: any age or gender, never a real
  or famous person.
- **Settings:** the Kurukshetra field at dawn, the chariot, a modern room or city at night.

Save the sheets in `style/refs/`. **CHECKPOINT:** show me the sheets. They are reused for every
Short until I change them.

## Phase 2: Story and shot list

Add an `animated` mode to `youtube_shorts.py` (`python youtube_shorts.py make --mode animated`).
Claude writes a **shot list**, not scenes. New structured output:

```
Shot: id, beat (cold_open|stakes|revelation|turn|resolution|silence),
      seconds (must be one of the video model's allowed durations, e.g. 4/6/8),
      shot_type (extreme wide / wide / medium / close-up / insert),
      subject (character ids from characters.json), action (one clear motion),
      camera (one move: orbit left, crane up, push in, dolly back, FPV dive ...),
      environment, lighting, transition_out (cut | whip | flash | match_cut | fade),
      vo_line (may be empty), on_screen_text (max 6 words, may be empty), sfx
```

Rules for the writer, added to the existing cinematic brief:
- Hook within the first 1.5 seconds: motion plus a question or a stakes line.
- 8–14 shots. Shot lengths follow `avg_shot_seconds` from the style bible. Any longer model clip
  gets trimmed in the edit.
- Two story threads intercut: Kurukshetra (Arjuna's crisis and Krishna's answer) and the modern
  person facing the same problem. They converge on the verse.
- **Keep all existing guardrails.** The only words given to Krishna are the provided verse
  translation, quoted exactly. Battle stays at a distance with no gore. Nothing sexualised, comedic
  or meme-like. No real people.
- The scripture reviewer must also review the shot list, including the visual descriptions.

## Phase 3: Generate the footage

For each shot:
1. Make a **hidden keyframe**: the image model with the style prompt plus the character reference
   image(s) plus the shot description. This is only an input. It must never appear in the final video.
2. Turn it into motion with image-to-video. The fal config currently has
   `fal-ai/veo3.1/fast/image-to-video`. **Before coding, check fal's current docs for this model's
   exact input fields, allowed durations, aspect ratios and price, and update `config.yaml` if
   they've changed.** Put a strong motion verb and the camera move first in the prompt.
3. For shots that should flow continuously, pass the previous clip's last frame (extract it with
   ffmpeg) as the next keyframe.
4. **Automatic quality check:** extract 3 frames per clip and send them to Claude (vision) with
   the character sheet. Score: identity match, anatomy (hands, faces, extra limbs), warped text or
   artefacts, respectful depiction, matches the shot. Regenerate a failing clip at most twice, then
   flag it for me instead of using it.
5. Reuse `SpendLedger`. **Before generating anything, print an itemised cost estimate**
   (keyframes × image price + total seconds × video price + likely retries) and ask me to confirm.
   Respect `daily_budget_usd`. Add a per-Short cap (default $8) and stop when it's reached.
6. Cache everything by a hash of the prompt and inputs, so re-running never pays twice for the same shot.

Add `--dry-run`: writes the shot list, prompts and cost estimate, and calls no paid API.

## Phase 4: Edit like the reference

Replace the stills renderer for animated mode with an ffmpeg edit:
- Trim each clip to its `seconds` and cut on the music's beats where possible.
- Transitions from `transition_out`. Use ffmpeg `xfade` for whip/flash/fade; match cuts are plain cuts.
- Optional speed ramps on action shots.
- A grade matched to the style bible: start from the existing `GRADE` and adjust to the
  reference palette. Keep grain and vignette subtle.
- **Kinetic captions:** use edge-tts word timings (`WordBoundary` events) to animate the voice-over
  captions word by word, in the text style from the style bible, inside YouTube Shorts' safe area
  (keep the bottom ~20% and right edge clear).
- **Audio:** voice-over, then music with sidechain ducking under the voice (`sidechaincompress`),
  then SFX from a `sfx/` folder I'll supply (royalty-free whooshes, impacts, ambience) placed by
  each shot's `sfx` field. Final loudness `loudnorm` to about −14 LUFS.
- Keep the "AI-generated" label on screen and `containsSyntheticMedia: True` on upload.

## Phase 5: Review and publish (CHECKPOINT on every Short)

- Save the full package: script, shot list, review, prompts, QC scores, cost and the final `short.mp4`.
- **Never upload without my approval.** After rendering, print the video path, the reviewer's
  scores, and the QC report. Upload only after I type `approve`. Upload as **private**, unless I
  pass `--publish-at`.
- Print the actual money spent for this Short (Claude tokens plus fal) next to the estimate.

## Engineering requirements

- Python, the Anthropic SDK and the existing model (`claude-opus-5-5`), with structured outputs
  like the existing code.
- Secrets only from env vars: `ANTHROPIC_API_KEY`, `FAL_KEY`. Never print or commit them.
- Tests with mocked Claude and mocked fal (see `tests/test_fal_media.py`). Cover the cost cap,
  the retry limit, cache hits, the dry run, and a short-list rejection by the reviewer.
- Keep the plain and cinematic stills modes working.
- Document the new flags in `README.md`.

## Acceptance checklist

- [ ] 100% moving footage. No still is visible on screen at any point.
- [ ] The same Krishna and Arjuna in every shot. QC identity score at least 8/10.
- [ ] First 1.5 seconds have motion plus a hook.
- [ ] The verse quote is exact and cited. The scripture review passed.
- [ ] Cost estimate shown and confirmed before generating. Per-Short and daily caps honoured.
- [ ] Captions readable on a phone and inside the safe area. Loudness about −14 LUFS.
- [ ] Uploaded only after I approve, as private, with the synthetic-media flag set.
