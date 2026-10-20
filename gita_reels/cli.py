from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path

from . import settings as settings_mod


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gita-reels", description="Cinematic, fully animated Bhagavad Gita reels")
    p.add_argument("--config", default="reels.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    # unattended
    s = sub.add_parser("plan", help="write and review a shot list and cost estimate; no video spend")
    s.add_argument("--episode")
    s = sub.add_parser("make", help="plan, generate, QC, edit, validate and package one reel")
    s.add_argument("--episode")
    sub.add_parser("autopilot", help="next episode in the storyline: make and (if enabled) publish")
    sub.add_parser("episodes", help="list the storyline and where the autopilot is")
    sub.add_parser("produce", help="(no Claude key) make takes for the queued, reviewed plan of the next episode")
    sub.add_parser("finish", help="(no Claude key) retake shots that failed review, or assemble and publish")

    # shot by shot
    s = sub.add_parser("new", help="create a project (episode or free concept) with a shot plan")
    s.add_argument("--episode")
    s.add_argument("--concept")
    s.add_argument("--character", default="")
    s.add_argument("--environment", default="")
    s.add_argument("--direction", default="", help="director's notes: style, lenses, mood, pacing")
    s.add_argument("--planner", choices=["auto", "claude", "template"], default="auto")
    s.add_argument("--plan-file", type=Path, help="import a plan JSON written elsewhere (see `brief`)")
    s.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                   help="per-project override, e.g. width=1080 height=1920 max_seconds=15 grade=dawn_soft")
    s = sub.add_parser("brief", help="print the planner prompt, to plan in a Claude chat without an API key")
    s.add_argument("--episode")
    s.add_argument("--concept")
    s.add_argument("--direction", default="")
    s = sub.add_parser("take", help="generate one new take of one shot (never overwrites other takes)")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int, help="shot number, from 1")
    s.add_argument("--provider", help="a fal model id, 'local' or 'animatic'; default: every fal model in order")
    s.add_argument("--hint", default="", help="extra direction for this take")
    s = sub.add_parser("import-take", help="use a clip you made yourself as a take")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int)
    s.add_argument("clip", type=Path)
    s = sub.add_parser("keyframe", help="use your own image as the starting frame for a shot's next takes")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int)
    s.add_argument("image", type=Path)
    s = sub.add_parser("select", help="approve a take for the edit")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int)
    s.add_argument("take")
    s.add_argument("--force", action="store_true", help="overrule a failed visual QC")
    s = sub.add_parser("reject", help="reject a take (it is kept on disk)")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int)
    s.add_argument("take")
    s = sub.add_parser("assemble", help="edit the selected takes, export and validate")
    s.add_argument("folder", type=Path)
    s = sub.add_parser("review-sheet", help="write one review image per take (keyframe + frames across the clip)")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int, nargs="?")
    s = sub.add_parser("qc", help="record a visual review of a take (pass or fail, with notes)")
    s.add_argument("folder", type=Path)
    s.add_argument("shot", type=int)
    s.add_argument("take")
    s.add_argument("verdict", choices=["pass", "fail"])
    s.add_argument("--note", action="append", default=[])
    s.add_argument("--reviewer", default="claude-code")
    s = sub.add_parser("advance", help="record an episode as done and move the storyline to the next one")
    s.add_argument("episode")
    s.add_argument("--status", default="published")
    s.add_argument("--url", default="")
    s = sub.add_parser("show", help="print a project's shots, takes and status")
    s.add_argument("folder", type=Path)

    # reference, checks, tools
    s = sub.add_parser("analyze", help="analyse a reference video: shots, contact sheet, cinematography report")
    s.add_argument("video", type=Path)
    s.add_argument("--out", type=Path)
    s.add_argument("--threshold", type=float, default=0.3, help="scene-cut sensitivity (lower = more cuts)")
    s.add_argument("--apply", action="store_true", help="merge the measurements into style/style_bible.json")
    s.add_argument("--no-claude", action="store_true", help="measurements only")
    s = sub.add_parser("validate", help="check an exported video's format, streams, black/frozen frames, audio")
    s.add_argument("video", type=Path)
    s.add_argument("--seconds", type=float)
    sub.add_parser("providers", help="check provider configuration and credentials (no spend)")
    s = sub.add_parser("studio", help="open the shot-by-shot web studio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s = sub.add_parser("publish", help="upload a finished reel folder")
    s.add_argument("folder", type=Path)
    s.add_argument("--privacy", choices=["public", "unlisted", "private"])
    s.add_argument("--publish-at")
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
    if args.cmd == "providers":
        from .providers import check
        print("\n".join(check(cfg)))
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
    if args.cmd == "validate":
        from .validate import validate_export
        from .analyze import probe
        info = probe(args.video)
        r = validate_export(args.video, info["width"], info["height"], round(info["fps"]),
                            args.seconds or info["duration"], expect_audio=bool(info["audio_codec"]))
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    if args.cmd == "analyze":
        import os

        from .analyze import analyze, apply_to_style
        out = args.out or Path("analysis") / args.video.stem
        claude = None
        if not args.no_claude and os.environ.get("ANTHROPIC_API_KEY"):
            from .writer import Claude
            claude = Claude(cfg)
        r = analyze(args.video, out, args.threshold, claude=claude)
        print(f"{r['shot_count']} shots, average {r['avg_shot_seconds']} s -> {out / 'report.md'}, "
              f"{out / 'contact_sheet.jpg'}")
        if args.apply:
            print(f"updated {apply_to_style(r, Path(cfg.paths.style))}")
        return 0
    if args.cmd == "studio":
        from .ui import serve
        serve(cfg, args.config, args.host, args.port)
        return 0

    from .pipeline import Studio
    from .project import Project
    studio = Studio(cfg)
    if args.cmd == "episodes":
        nxt = studio.state()["next"]
        for i, e in enumerate(cfg.episodes()):
            print(f"{'->' if i == nxt % len(cfg.episodes()) else '  '} {e['id']} ch{e['chapter']:>2} {e['title']}")
    elif args.cmd == "plan":
        folder = studio.make(args.episode, dry_run=True)
        meta = json.loads((folder / "project.json").read_text(encoding="utf-8"))
        print(json.dumps({"folder": str(folder), "status": meta["status"], "title": meta["plan"]["title"],
                          "shots": len(meta["plan"]["shots"]), "review": (meta.get("review") or {}).get("verdict"),
                          "notes": meta.get("plan_notes", []), "fal_estimate_usd": meta["estimate_usd"]["fal"],
                          "claude_usd": meta.get("claude_usd")}, indent=2))
    elif args.cmd == "make":
        folder = studio.make(args.episode)
        print(json.dumps(json.loads((folder / "project.json").read_text(encoding="utf-8"))["status"]), folder)
    elif args.cmd == "autopilot":
        print(json.dumps(studio.autopilot(), indent=2))
    elif args.cmd == "produce":
        print(json.dumps(studio.produce(), indent=2))
    elif args.cmd == "finish":
        print(json.dumps(studio.finish_pending(), indent=2))
    elif args.cmd == "new":
        brief = {"concept": args.concept, "character": args.character, "environment": args.environment} \
            if args.concept else None
        opts = dict(kv.split("=", 1) for kv in args.option)
        proj = studio.create(args.episode, brief, opts, args.direction, args.planner, args.plan_file)
        _show(proj)
    elif args.cmd == "advance":
        st = studio.advance(args.episode, {"status": args.status, **({"url": args.url} if args.url else {})})
        print(f"next episode: {cfg.episodes()[st['next'] % len(cfg.episodes())]['id']}")
    elif args.cmd == "brief":
        from .writer import Claude
        ep = ({"kind": "concept", "id": "concept", "chapter": 0, "title": args.concept[:60], "story": args.concept,
               "set_pieces": [], "verses": []} if args.concept else studio.episode(args.episode))
        system, prompt = Claude(cfg).plan_request(ep, studio.verse_for(ep), direction=args.direction)
        from .writer import PlanReview, ReelPlan
        print(f"=== SYSTEM ===\n{system}\n\n=== REQUEST ===\n{prompt}\n\n=== REPLY FORMAT ===\nReply with only "
              f"JSON {{\"plan\": <plan>, \"review\": <review>}}, where plan matches the first schema and review "
              f"(an independent check of fidelity, respect and craft) matches the second. Save it and run "
              f"`gita-reels new --episode <id> --plan-file plan.json`:\n{json.dumps(ReelPlan.model_json_schema())}\n"
              f"{json.dumps(PlanReview.model_json_schema())}")
    else:
        proj = Project(args.folder, cfg)
        i = getattr(args, "shot", 1) - 1
        if args.cmd == "take":
            t = studio.take(proj, i, args.provider, args.hint)
            print(json.dumps({k: t.get(k) for k in ("id", "status", "provider", "model", "cost_usd", "error",
                                                     "retry_hint", "metrics")}, indent=2))
        elif args.cmd == "import-take":
            t = studio.import_take(proj, i, args.clip)
            print(json.dumps({k: t.get(k) for k in ("id", "status", "metrics")}, indent=2))
        elif args.cmd == "keyframe":
            proj.shot_dir(i).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.image, proj.shot_dir(i) / "keyframe_user.png")
            print(f"shot {i + 1} will start from {args.image}")
        elif args.cmd == "select":
            proj.select(i, args.take, args.force)
        elif args.cmd == "reject":
            proj.reject(i, args.take)
        elif args.cmd == "assemble":
            meta = studio.finish(proj)
            print(json.dumps({"status": meta["status"], "final": meta["edit"]["final"],
                              "validation": meta["validation"]}, indent=2))
        elif args.cmd == "show":
            _show(proj)
        elif args.cmd == "review-sheet":
            shots = [args.shot - 1] if args.shot else range(len(proj.plan().shots))
            for k in shots:
                for t in proj.takes(k):
                    if t.get("clip") and t["status"] in ("ready", "failed_qc"):
                        print(proj.review_sheet(k, t["id"]))
        elif args.cmd == "qc":
            t = proj.record_qc(i, args.take, args.verdict == "pass", args.note, args.reviewer)
            print(t["status"])
    return 0


def _show(proj) -> None:
    meta = proj.meta()
    print(f"{proj.folder}  [{meta['status']}]  planner={meta.get('planner')}  "
          f"estimate ${meta.get('estimate_usd', {}).get('fal')}")
    for note in meta.get("plan_notes", []):
        print(f"  note: {note}")
    for i, sh in enumerate(meta["plan"]["shots"]):
        sel = meta["selected"].get(str(i))
        takes = ", ".join(f"{t['id']}:{t['status']}" + ("*" if t["id"] == sel else "") for t in proj.takes(i))
        print(f"  {i + 1:>2}. {sh['purpose']:<16} {sh['lens']:<12} {sh['move']:<18} {sh['lighting']:<17} "
              f"{sh['on_screen_seconds']:.1f}s  takes: {takes or '-'}")


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
                                                   ("A3 voice", "voice.wav"), ("A4 sfx", "sfx.wav"))
             if (folder / "audio" / f).exists()}
    write_timeline(meta["plan"]["title"], cfg.reel.fps, shots, titles, folder / "titles" / "brand.png", stems,
                   total, folder)
    print(f"rewrote {folder / 'timeline.otio'} and {folder / 'timeline.xml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
