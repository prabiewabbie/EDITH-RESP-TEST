# Gita Reels: fully animated, editable reels on autopilot

`gita_reels/` makes 15–30 second vertical reels that tell the Bhagavad Gita in order, in the style
measured from the reference reel (`style/style_bible.json`): photoreal cinematic CGI, golden-hour
backlight, ~2 s shots, smooth camera, varied shot scale, music-led, almost no text.

Every second on screen is generated video. Stills exist only as hidden keyframes fed into the video model.

## Pipeline

1. **Storyline:** `style/episodes.json` has 26 episodes from Dhritarashtra's question (1.1) to Sanjaya's
   last words (18.78). The autopilot takes the next one each day.
2. **Plan:** Claude writes a 6–12 shot list to the style guide. Code then enforces the length (15–30 s),
   at most one sideways widescreen shot, and a verse line only if it's a word-for-word excerpt of `data/verses.json`.
3. **Review:** a separate Claude call checks scripture fidelity and respect. If the review fails, the
   reel is dropped before any video is paid for.
4. **Characters:** one reference sheet per character (`style/refs/`) is made once and passed into
   every keyframe, so Krishna and Arjuna look the same across shots and reels.
5. **Footage:** each shot gets a keyframe, which Claude checks, then image-to-video, whose frames
   Claude checks again. A failing shot gets up to 2 retries with a fix hint. If the first video
   model fails, the next one in `reels.yaml` is tried.
6. **Edit:** clips are trimmed, conformed to 1080×1920 at 24 fps and lightly graded. Flash cuts,
   the verse title, the channel handle, the "AI-generated" label, music, the model's own ambient
   sound and an optional voice-over are added, mixed and loudness-normalised to −14 LUFS.
7. **Publish:** the reel is uploaded to YouTube with the synthetic-media label, using `publish.privacy`.

## Cost controls

- Each run prints an estimate, and `gita-reels plan` shows it without spending on video.
- `reel_budget_usd` (default $8) and `daily_budget_usd` (default $15) are hard stops.
- Every fal result is cached by its exact input, so re-running a reel doesn't pay twice.

A typical 20-second reel is 8 shots × 4 s generated × $0.10/s, plus 8 keyframes × $0.08, plus
retries: about **$4–6**. Claude adds about $0.50–1. Check fal's current prices with `check-models`.

## The editable project (one folder per reel)

| Path | What it is |
|---|---|
| `clips_raw/` | untouched video-model output, every attempt |
| `clips/shot_NN.mp4` | conformed shots exactly as used in the edit |
| `titles/verse.png`, `titles/brand.png` | transparent text layers you can replace |
| `audio/music.wav`, `ambience.wav`, `voice.wav`, `mix.wav` | separate audio stems |
| `captions.srt` | verse line and narration with timings |
| `timeline.xml` | FCP 7 XML. In Premiere Pro: File > Import. In DaVinci Resolve: File > Import > Timeline |
| `timeline.otio` | OpenTimelineIO, for Resolve and other OTIO tools |
| `final.mp4` | the flattened master that was published |
| `project.json` | plan, review, QC scores, models used, cost |

After copying a reel folder to another computer, run `gita-reels timeline <folder>` to rewrite the
timelines with that machine's file paths.

## Commands

```bash
pip install -e ".[reels]"          # plus ffmpeg on your PATH
gita-reels check-models            # compare fal model fields with fal's live schema
gita-reels episodes                # storyline and the next episode
gita-reels plan --episode e04      # shot list + review + cost estimate; no video spend
gita-reels make --episode e04      # full reel into reels/<time>-e04/
gita-reels publish reels/<folder>  # upload one reel
gita-reels autopilot               # next episode: make, then publish if auto_publish is on
gita-reels youtube-auth            # one-time; save the printed JSON as the YOUTUBE_TOKEN_JSON secret
```

## Automation (GitHub Actions)

`.github/workflows/reels.yml` runs `autopilot` every day. It uploads the whole editable project
as a downloadable artifact (kept 30 days), and keeps the storyline position, spend ledger,
character sheets and cache on the `reels-state` branch. Repository secrets it needs:
`ANTHROPIC_API_KEY`, `FAL_KEY`, `YOUTUBE_TOKEN_JSON`. Without the first two it skips quietly.
