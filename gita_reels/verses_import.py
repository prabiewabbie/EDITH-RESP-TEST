"""Build data/verses.json from public-domain sources.

English: Annie Besant's translation (4th edition, 1922; public domain, Besant died 1933), as proofread on
Wikisource, with its Devanagari text. Transliteration: the gita/gita dataset (Unlicense). The text is kept
exactly as published (Besant's spelling, e.g. "Pârtha"); only footnote markers are removed.

Run where wikisource.org is reachable (the `import verses` workflow does this):
    python -m gita_reels.verses_import data/verses.json
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

WIKISOURCE = ("https://en.wikisource.org/w/api.php?action=parse&page=Bhagavad-Gita_(Besant_4th)/Discourse_{n}"
              "&prop=text&format=json&formatversion=2")
PAGE = "https://en.wikisource.org/wiki/Bhagavad-Gita_(Besant_4th)/Discourse_{n}"
GITA_DATA = "https://raw.githubusercontent.com/gita/gita/main/data/verse.json"
SOURCE = "Annie Besant (tr.), The Bhagavad-Gita, 4th ed. 1922, public domain"
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
MARKER = re.compile(r"\((\d+)(?:\s*[-–]\s*(\d+))?\)\s*$")


class _Discourse(HTMLParser):
    """Walks a rendered Discourse page: Sanskrit blocks (lang="sa") and English paragraphs ending in "(N)"."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0            # div nesting
        self.sa_depth = None      # depth at which the current Sanskrit block opened
        self.skip = 0             # inside <style>, footnote <sup>, page-number spans, footnote list
        self.skip_tags: list[str] = []
        self.sa_text, self.en, self.para = "", "", ""
        self.sanskrit: dict[int, str] = {}
        self.english: dict[int, str] = {}
        self.ranges: dict[int, tuple[int, int]] = {}
        self.started = False      # English is collected only after the first Sanskrit verse

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if self.skip:
            if tag == self.skip_tags[-1]:
                self.skip += 1
            return
        if tag in ("style", "script") or (tag == "sup" and "reference" in cls) or "pagenum" in cls \
                or (tag in ("ol", "div") and ("references" in cls or "reflist" in cls)):
            self.skip, self.skip_tags = 1, self.skip_tags + [tag]
            return
        if tag == "div":
            self.depth += 1
            if a.get("lang") == "sa" and self.sa_depth is None:
                self.sa_depth, self.sa_text = self.depth, ""
        if tag == "br":
            self._text("\n")
        if tag == "p" and self.sa_depth is None:
            self.para = ""

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip_tags[-1]:
                self.skip -= 1
                if not self.skip:
                    self.skip_tags.pop()
            return
        if tag == "p" and self.sa_depth is not None:
            self.sa_text += "\n"
        elif tag == "p":
            self._paragraph(self.para)
        if tag == "div":
            if self.sa_depth == self.depth:
                self._sanskrit(self.sa_text)
                self.sa_depth = None
            self.depth -= 1

    def handle_data(self, data):
        if not self.skip:
            self._text(data)

    def _text(self, s):
        if self.sa_depth is not None:
            self.sa_text += s
        else:
            self.para += s

    def _sanskrit(self, text):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        m = re.search(r"॥\s*([०-९0-9]+)\s*॥", " ".join(lines))
        if m:
            self.sanskrit[int(m.group(1).translate(DEVANAGARI_DIGITS))] = "\n".join(lines)
            self.started = True

    def _paragraph(self, text):
        text = re.sub(r"\s+", " ", unescape(text)).strip()
        if not self.started or not text:
            return
        self.en = f"{self.en} {text}".strip()
        m = MARKER.search(self.en)
        if m:
            first, last = int(m.group(1)), int(m.group(2) or m.group(1))
            body = self.en[:m.start()].strip()
            for v in range(first, last + 1):
                self.english[v] = body
                if last != first:
                    self.ranges[v] = (first, last)
            self.en = ""


def parse_discourse(html: str) -> dict[int, dict]:
    p = _Discourse()
    p.feed(html)
    out = {}
    for v, text in p.english.items():
        out[v] = {"translation": text, "sanskrit": p.sanskrit.get(v, "")}
        if v in p.ranges:
            out[v]["translated_with"] = "{}-{}".format(*p.ranges[v])
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "gita-studio-verse-import/1.0 (public-domain text)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def build() -> tuple[list[dict], list[str]]:
    translit = {(v["chapter_number"], v["verse_number"]): v["transliteration"].strip()
                for v in json.loads(_get(GITA_DATA))}
    verses, notes = [], []
    for ch in range(1, 19):
        html = json.loads(_get(WIKISOURCE.format(n=ch)))["parse"]["text"]
        parsed = parse_discourse(html)
        if not parsed:
            notes.append(f"chapter {ch}: no verses parsed")
        expected = max(v for c, v in translit if c == ch)
        missing = [v for v in range(1, max(parsed, default=0) + 1) if v not in parsed]
        if missing:
            notes.append(f"chapter {ch}: missing {missing}")
        if max(parsed, default=0) != expected:
            notes.append(f"chapter {ch}: {max(parsed, default=0)} verses (gita/gita numbering has {expected})")
        for v in sorted(parsed):
            e = parsed[v]
            verses.append({"chapter": ch, "verse": v, "sanskrit": e["sanskrit"],
                           "transliteration": translit.get((ch, v), ""), "translation": e["translation"],
                           "source": SOURCE, "url": PAGE.format(n=ch),
                           **({"translated_with": e["translated_with"]} if "translated_with" in e else {})})
    return verses, notes


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "data/verses.json")
    verses, notes = build()
    out.write_text(json.dumps(verses, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(verses)} verses to {out}")
    for n in notes:
        print("note:", n)
    episodes = Path("style/episodes.json")
    if episodes.exists():
        have = {f"{v['chapter']}.{v['verse']}" for v in verses if v["translation"]}
        need = {r for e in json.loads(episodes.read_text(encoding="utf-8")) for r in e.get("verses", [])}
        if need - have:
            print("ERROR: episode verses missing:", sorted(need - have))
            return 1
        print(f"all {len(need)} verses used by the episodes are present")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
