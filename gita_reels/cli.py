from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import settings as settings_mod


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gita-reels", description="Fully animated Bhagavad Gita reels")
    p.add_argument("--config", default="reels.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan", help="write and review a shot list and cost estimate; no video spend")
    s.add_argument("--episode")
    s = sub.add_parser("make", help="plan, generate, QC, edit and package one reel")
    s.add_argument("--episode")
    s = sub.add_parser("publish", help="upload a finished reel folder")
    s.add_argument("folder", type=Path)
    s.add_argument("--privacy", choices=["public", "unlisted", "private"])
    s.add_argument("--publish-at")
    sub.add_parser("autopilot", help="next episode in the storyline: make and (if enabled) publish")
    sub.add_parser("episodes", help="list the storyline and where the autopilot is")
    sub.add_parser("check-models", help="compare configured fal fields with fal's live API schema")
    sub.add_parser("youtube-auth", help="one-time YouTube login; prints the token for YOUTUBE_TOKEN_JSON")
    s = sub.add_parser("timeline", help="rewrite timeline.otio/.xml with paths on this machine")
    s.add_argument("folder", type=Path)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = settings_mod.load(args.config)

    if args.cmd == "check-models":
        from .fal import check_models
        print("\n".join(check_models(cfg)))
        return 0
    if args.cmd == "youtube-auth":
        from .publish import authorize
        print(authorize())
        return 0
    if args.cmd == "publish":
        from .publish import upload
        print(upload(args.folder, args.privacy or cfg.publish.privacy, args.publish_at))
        return 0
    if args.cmd == "timeline":
        return _relink(cfg, args.folder)

    from .pipeline import Studio
    studio = Studio(cfg)
    if args.cmd == "episodes":
        nxt = studio.state()["next"]
        for i, e in enumerate(cfg.episodes()):
            print(f"{'->' if i == nxt % len(cfg.episodes()) else '  '} {e['id']} ch{e['chapter']:>2} {e['title']}")
    elif args.cmd == "plan":
        folder = studio.make(args.episode, dry_run=True)
        meta = json.loads((folder / "project.json").read_text(encoding="utf-8"))
        print(json.dumps({"folder": str(folder), "status": meta["status"], "title": meta["plan"]["title"],
                          "shots": len(meta["plan"]["shots"]), "review": meta["review"]["verdict"],
                          "fal_estimate_usd": meta["estimate_usd"]["fal"], "claude_usd": meta.get("claude_usd")},
                         indent=2))
    elif args.cmd == "make":
        folder = studio.make(args.episode)
        print(json.dumps(json.loads((folder / "project.json").read_text(encoding="utf-8"))["status"]), folder)
    elif args.cmd == "autopilot":
        print(json.dumps(studio.autopilot(), indent=2))
    return 0


def _relink(cfg, folder: Path) -> int:
    """Rebuild timelines after copying a reel folder to an editing machine."""
    from .edit import write_timeline
    meta = json.loads((folder / "project.json").read_text(encoding="utf-8"))
    shots = [(folder / "clips" / f"shot_{i + 1:02d}.mp4", sh["on_screen_seconds"])
             for i, sh in enumerate(meta["plan"]["shots"])]
    total = sum(s for _, s in shots)
    titles = []
    if (folder / "titles" / "verse.png").exists():
        i = meta["plan"]["verse_shot_index"]
        start = sum(s for _, s in shots[:i])
        titles.append((folder / "titles" / "verse.png", start, min(total, start + max(shots[i][1], 2.5))))
    stems = {k: folder / "audio" / f for k, f in (("A1 music", "music.wav"), ("A2 ambience", "ambience.wav"),
                                                   ("A3 voice", "voice.wav")) if (folder / "audio" / f).exists()}
    write_timeline(meta["plan"]["title"], cfg.reel.fps, shots, titles, folder / "titles" / "brand.png", stems,
                   total, folder)
    print(f"rewrote {folder / 'timeline.otio'} and {folder / 'timeline.xml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
