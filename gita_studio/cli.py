from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config
from .scripture import TOTAL_VERSES, import_verses


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gita", description="Automated Bhagavad Gita content studio")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import-verses", help="Import a translation (CSV or JSON) into the corpus")
    s.add_argument("file")
    s.add_argument("--source", help="Attribution to store on every verse, e.g. 'Edwin Arnold, 1885 (public domain)'")

    s = sub.add_parser("create", help="Generate, review and package new posts")
    s.add_argument("-n", "--count", type=int)

    sub.add_parser("queue", help="List posts waiting for human review")
    s = sub.add_parser("show", help="Print a post's full package")
    s.add_argument("post_id", type=int)
    s = sub.add_parser("approve")
    s.add_argument("post_ids", type=int, nargs="+")
    s = sub.add_parser("reject")
    s.add_argument("post_id", type=int)
    s.add_argument("reason")

    s = sub.add_parser("schedule", help="Assign approved posts to upcoming slots")
    s.add_argument("--days", type=int, default=14)
    sub.add_parser("publish", help="Publish every scheduled post that is due")
    s = sub.add_parser("import-metrics", help="Load platform analytics from CSV")
    s.add_argument("file")
    sub.add_parser("strategy", help="Refresh the strategy brief from analytics")
    sub.add_parser("run-daily", help="create + schedule + publish (what the cron job runs)")
    sub.add_parser("status", help="Counts by status")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)

    if args.cmd == "import-verses":
        n = import_verses(args.file, config.paths.verses, args.source)
        print(f"imported {n} verses into {config.paths.verses} (Gita has {TOTAL_VERSES})")
        return 0

    from .pipeline import Studio  # deferred: needs the corpus and API credentials
    studio = Studio(config)

    if args.cmd == "create":
        print(json.dumps({"created": studio.create_posts(args.count)}))
    elif args.cmd == "queue":
        for r in studio.store.posts("needs_review"):
            pkg = json.loads(r["package"])
            print(f"#{r['id']:>4}  BG {r['chapter']}.{r['verse']:<3} {r['format']:<12} "
                  f"fidelity={r['fidelity']}  {pkg['script']['title']}")
    elif args.cmd == "show":
        row = studio.store.get_post(args.post_id)
        if not row:
            print("not found", file=sys.stderr)
            return 1
        print(json.dumps({**dict(row), "package": json.loads(row["package"])}, ensure_ascii=False, indent=2))
    elif args.cmd == "approve":
        for pid in args.post_ids:
            studio.approve(pid)
        print(f"approved {args.post_ids}")
    elif args.cmd == "reject":
        studio.reject(args.post_id, args.reason)
    elif args.cmd == "schedule":
        for pid, at in studio.schedule(days=args.days):
            print(f"#{pid} -> {at}")
    elif args.cmd == "publish":
        print(json.dumps({"published": studio.publish_due()}))
    elif args.cmd == "import-metrics":
        print(f"imported {studio.import_metrics(args.file)} rows")
    elif args.cmd == "strategy":
        print(json.dumps(studio.refresh_strategy(), ensure_ascii=False, indent=2))
    elif args.cmd == "run-daily":
        print(json.dumps(studio.run_daily()))
    elif args.cmd == "status":
        rows = studio.store.db.execute("SELECT status, COUNT(*) n FROM posts GROUP BY status").fetchall()
        print(json.dumps({r["status"]: r["n"] for r in rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
