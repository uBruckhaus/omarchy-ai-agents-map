#!/usr/bin/env python3
"""Keep the translated manuals (manual.<lang>.html) in step with the English manual.html.

Each translation carries, in one comment at its top, a fingerprint of every English section it was
translated from (the chapter intros and every <h2> part). When the English text of a section
changes, its fingerprint no longer matches and the section is listed as to be translated again.

    docs/manual/translations.py check [lang …]          what changed in English since; exit 1 if anything
    docs/manual/translations.py stamp <lang> [id …]     after translating: record today's English
                                                        (all sections, or only the ids given)

check also compares the markup with the English (section ids, images, the screenshot's numbered
pins, the contents marker), which every translation must keep as it is.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGLISH = HERE / "manual.html"
LANGUAGES = ("de", "es", "fr", "it", "nl", "pl", "pt")
STAMP = re.compile(r"<!-- en-sections: (\{.*?\}) -->\n?")
BLOCK_START = re.compile(r'<section class="(?P<cls>[^"]*)"(?: id="(?P<sid>[^"]+)")?>|<h2 id="(?P<hid>[^"]+)">')


def source(lang: str) -> Path:
    return ENGLISH if lang == "en" else HERE / f"manual.{lang}.html"


def blocks(text: str) -> dict[str, str]:
    """{section id: its text} in document order: a <section> up to its first <h2> (the cover has no
    id: "cover"), and each <h2> part up to the next part."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)        # comments (notes, this stamp) are no content
    marks = list(BLOCK_START.finditer(text))
    out = {}
    for i, match in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = match.group("hid") or match.group("sid") or match.group("cls").split()[0]
        out[key] = text[match.start():end]
    return out


def fingerprint(fragment: str) -> str:
    """The English text of a part, whitespace folded: only a real change counts."""
    return hashlib.sha256(re.sub(r"\s+", " ", fragment).strip().encode()).hexdigest()[:12]


def english_prints() -> dict[str, str]:
    return {key: fingerprint(text) for key, text in blocks(ENGLISH.read_text()).items()}


def stamped(text: str) -> dict[str, str] | None:
    match = STAMP.search(text)
    return json.loads(match.group(1)) if match else None


def markup(text: str) -> dict:
    """What a translation keeps exactly as the English has it."""
    text = STAMP.sub("", text)
    return {"ids": re.findall(r'\sid="([^"…]+)"', text),
            "images": re.findall(r'src="([^"]+)"', text),
            "pins": re.findall(r'<span class="pin"[^>]*>', text),
            "toc": text.count("<!-- TOC -->"),
            "lang": re.search(r'<html lang="([^"]+)"', text).group(1)}


def report(lang: str) -> tuple[list, list, list, list]:
    """(changed, new, removed, markup problems) of one translation."""
    text = source(lang).read_text()
    now, then = english_prints(), stamped(text) or {}
    changed = [k for k in now if k in then and then[k] != now[k]]
    new = [k for k in now if k not in then]
    removed = [k for k in then if k not in now]
    en, tr = markup(ENGLISH.read_text()), markup(text)
    problems = [f"{key} differ from the English" for key in ("ids", "images", "pins", "toc") if en[key] != tr[key]]
    if tr["lang"] != lang:
        problems.append(f'<html lang="{tr["lang"]}">, not "{lang}"')
    if stamped(text) is None:
        problems.append("no en-sections stamp (run: translations.py stamp " + lang + ")")
    return changed, new, removed, problems


def check(langs) -> int:
    stale = False
    for lang in langs:
        changed, new, removed, problems = report(lang)
        if not (changed or new or removed or problems):
            print(f"{lang}: up to date")
            continue
        stale = True
        print(f"{lang}:")
        for label, items in (("changed in English", changed), ("new in English", new),
                             ("gone from English", removed), ("markup", problems)):
            if items:
                print(f"  {label}: {', '.join(items)}")
    return 1 if stale else 0


def stamp(lang: str, only=()) -> int:
    path = source(lang)
    text = path.read_text()
    prints = english_prints()
    record = dict(stamped(text) or {})
    unknown = [k for k in only if k not in prints]
    if unknown:
        print(f"no such section: {', '.join(unknown)}", file=sys.stderr)
        return 2
    record.update({k: prints[k] for k in (only or prints)})
    record = {k: record[k] for k in prints if k in record}      # English order, gone ones dropped
    line = f"<!-- en-sections: {json.dumps(record, separators=(',', ':'))} -->\n"
    text = STAMP.sub("", text)
    head = text.index("<head>")
    path.write_text(text[:head] + line + text[head:])
    print(f"{lang}: {len(only) or len(prints)} section(s) stamped")
    return 0


def main(argv) -> int:
    if not argv or argv[0] not in ("check", "stamp"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "check":
        return check(argv[1:] or LANGUAGES)
    if len(argv) < 2 or argv[1] not in LANGUAGES:
        print("stamp <lang> [section id …]", file=sys.stderr)
        return 2
    return stamp(argv[1], argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
