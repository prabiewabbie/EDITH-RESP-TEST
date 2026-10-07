"""The source of truth for verse text.

Claude is never asked to recall a verse from memory. Every verse quoted in a post is
loaded from the corpus file and passed to the model, and the reviewer checks the
output against that exact text.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from pydantic import BaseModel

# Verse counts per chapter in the standard 700-verse recension.
CHAPTERS: dict[int, tuple[str, int]] = {
    1: ("Arjuna Vishada Yoga", 47),
    2: ("Sankhya Yoga", 72),
    3: ("Karma Yoga", 43),
    4: ("Jnana Karma Sannyasa Yoga", 42),
    5: ("Karma Sannyasa Yoga", 29),
    6: ("Dhyana Yoga", 47),
    7: ("Jnana Vijnana Yoga", 30),
    8: ("Akshara Brahma Yoga", 28),
    9: ("Raja Vidya Raja Guhya Yoga", 34),
    10: ("Vibhuti Yoga", 42),
    11: ("Vishvarupa Darshana Yoga", 55),
    12: ("Bhakti Yoga", 20),
    13: ("Kshetra Kshetrajna Vibhaga Yoga", 34),  # 35 in editions that include the Arjuna-uvacha opening verse
    14: ("Gunatraya Vibhaga Yoga", 27),
    15: ("Purushottama Yoga", 20),
    16: ("Daivasura Sampad Vibhaga Yoga", 24),
    17: ("Shraddhatraya Vibhaga Yoga", 28),
    18: ("Moksha Sannyasa Yoga", 78),
}
TOTAL_VERSES = sum(n for _, n in CHAPTERS.values())


class Verse(BaseModel):
    chapter: int
    verse: int
    sanskrit: str
    transliteration: str = ""
    translation: str
    source: str  # translation attribution, shown on every post

    @property
    def ref(self) -> str:
        return f"BG {self.chapter}.{self.verse}"


class Corpus:
    def __init__(self, verses: list[Verse]):
        self._by_ref = {(v.chapter, v.verse): v for v in verses}

    @classmethod
    def load(cls, path: str | Path) -> "Corpus":
        path = Path(path)
        if not path.exists():
            sample = path.with_name("verses.sample.json")
            raise FileNotFoundError(
                f"{path} not found. Import a translation you have rights to with "
                f"`gita import-verses <file>`, or copy {sample} to get started."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([Verse.model_validate(v) for v in data])

    def get(self, chapter: int, verse: int) -> Verse | None:
        return self._by_ref.get((chapter, verse))

    def all(self) -> list[Verse]:
        return [self._by_ref[k] for k in sorted(self._by_ref)]

    def __len__(self) -> int:
        return len(self._by_ref)


def validate_ref(chapter: int, verse: int) -> bool:
    return chapter in CHAPTERS and 1 <= verse <= CHAPTERS[chapter][1]


def import_verses(src: str | Path, dest: str | Path, source: str | None = None) -> int:
    """Import verses from CSV or JSON into the corpus file.

    CSV columns: chapter, verse, sanskrit, transliteration, translation[, source]
    """
    src = Path(src)
    if src.suffix.lower() == ".csv":
        with open(src, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    else:
        rows = json.loads(src.read_text(encoding="utf-8"))

    verses: list[Verse] = []
    for row in rows:
        row = dict(row)
        row["chapter"], row["verse"] = int(row["chapter"]), int(row["verse"])
        if source:
            row["source"] = source
        if not row.get("source"):
            raise ValueError(f"Verse {row['chapter']}.{row['verse']} has no source; pass --source")
        if not validate_ref(row["chapter"], row["verse"]):
            raise ValueError(f"Invalid reference {row['chapter']}.{row['verse']}")
        verses.append(Verse.model_validate(row))

    dest = Path(dest)
    existing = Corpus.load(dest).all() if dest.exists() else []
    merged = {(v.chapter, v.verse): v for v in existing + verses}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps([merged[k].model_dump() for k in sorted(merged)], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(verses)
