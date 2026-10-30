#!/usr/bin/env bash
# No-key, no-spend smoke run of both CLIs. Run from the repo root with the venv active:
#   bash .claude/skills/run-gita-studio/smoke.sh [episode]
# 1. gita-reels: template plan -> animatic take per shot -> select -> assemble -> validate (writes reels/<id>/)
# 2. gita (posts studio): full create/approve/schedule/publish flow with the tests' FakeClaude, in a temp dir
set -euo pipefail
EP=${1:-e01}

echo "== gita-reels providers"
gita-reels providers

echo "== gita-reels new --episode $EP (template planner, animatic provider, 1080x1920)"
out=$(gita-reels new --episode "$EP" --planner template --option provider=animatic)
echo "$out"
P=$(printf '%s\n' "$out" | awk 'NR==1{print $1}')
n=$(printf '%s\n' "$out" | grep -cE '^ +[0-9]+\. ')

for i in $(seq 1 "$n"); do
  gita-reels take "$P" "$i" --provider animatic > /dev/null
  gita-reels select "$P" "$i" t01 > /dev/null
done
gita-reels show "$P"

echo "== assemble"
gita-reels assemble "$P" > /dev/null || true   # exit code is not the verdict; project.json is
python - "$P" <<'PY'
import json, sys
m = json.load(open(f"{sys.argv[1]}/project.json"))
bad = [f"{c['name']}: {c['detail']}" for c in m["validation"]["checks"] if not c["ok"]]
print("status", m["status"], "| validation", "ok" if m["validation"]["ok"] else bad)
sys.exit(0 if m["validation"]["ok"] else 1)
PY
ffmpeg -loglevel error -y -ss 4 -i "$P/final.mp4" -frames:v 1 "$P/frame_4s.png"
echo "final: $P/final.mp4  frame: $P/frame_4s.png"

echo "== gita posts pipeline with FakeClaude"
python - <<'PY'
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from test_pipeline import FakeClaude
from gita_studio.config import load_config
from gita_studio.pipeline import Studio
from gita_studio.scripture import Corpus
from gita_studio.store import Store

tmp = Path(tempfile.mkdtemp())
cfg = load_config("config.yaml")
cfg.paths.db, cfg.paths.outbox, cfg.paths.media = str(tmp / "t.db"), str(tmp / "outbox"), str(tmp / "media")
# the sample corpus, not data/verses.json: FakeClaude's canned 2.47 quote only matches the sample's wording
s = Studio(cfg, claude=FakeClaude(), store=Store(cfg.paths.db), corpus=Corpus.load("data/verses.sample.json"))
[pid] = s.create_posts(1)
print("created", pid, s.store.get_post(pid)["status"])
s.approve(pid)
[(pid, slot)] = s.schedule()
from datetime import datetime, timedelta
print("published", s.publish_due(at=datetime.fromisoformat(slot) + timedelta(minutes=1)))
print("outbox", sorted(p.name for p in (tmp / "outbox").rglob("*.json")))
PY
echo "SMOKE OK"
