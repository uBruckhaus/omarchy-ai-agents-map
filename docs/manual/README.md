# User manual

The user manual of the AI Agents Map Omarchy plugin, one PDF per interface language (8). It started
as a copy of Maprisma's manual (2026-11-11, plugin 1.0.0); the plugin's own parts (bar button,
marketplace install, launcher entry) replace Maprisma's install chapter.

| File | What it is |
| --- | --- |
| `manual.html` | The English text. Edit this. |
| `manual.<lang>.html` | The translations (de, es, fr, it, nl, pl, pt), same markup and ids. |
| `translations.py` | `check` lists English sections changed since a translation; `stamp <lang>` after translating. |
| `manual.css` | The print layout (A4, cover, contents, callouts, tables). |
| `images/` | Screenshots and the icon used in the manual. |
| `build.py` | Builds `assets/user-manual[.<lang>].pdf` (Help opens the one of the interface language). |

## Build

```sh
python3 docs/manual/build.py --all                    # every language; version from manifest.json
python3 docs/manual/build.py --lang de                # one language
python3 docs/manual/build.py --out ~/Documents/AI-Agents-Map-User-Manual.pdf
```

Needs `chromium` (or `google-chrome-stable`), `pdftotext` (poppler) and `pdfcraft-cli`
(on PATH, in `/mnt/Data/Ai/McpServers/pdfcraft-*/` or `~/Applications/pdfcraft-*/`, or `PDFCRAFT=/path/to/pdfcraft-cli`).

What the build does:

1. numbers chapters and sections, fills in the contents page and the version (from
   `manifest.json`) and the date;
2. prints the HTML with Chromium, reads with `pdftotext` on which page each heading landed, and
   prints again with those page numbers until they no longer change;
3. runs one `pdfcraft-cli run --script` (the steps are kept in `build/finish.json`): document info,
   page labels (i–iii for the front matter, 1… from chapter 1), a running header with the chapter
   and a footer with the page number, bookmarks for every chapter and section, and opening with
   the bookmarks panel at page width.

## Keeping it up to date

- **A new feature:** add a paragraph to its chapter, or a new `<h2 id="…">` section. The contents,
  bookmarks and numbers follow by themselves.
- **A new chapter:** `<section class="chapter" id="…"><h1>Title</h1> … </section>` before the
  Reference appendix (`class="chapter appendix"`).
- **Every language:** change all 8 files in the same way, then `translations.py stamp <lang>`.
- **Screenshots:** replace the files in `images/` (same names), or add one in a
  `<div class="figure wide"><img src="images/…" alt="…"><p class="caption">…</p></div>`. Always
  give `alt`. The numbered markers on the main window (`.pin`, `left`/`top` in percent of the
  picture) must be moved when that screenshot changes.
- **Check after building:** the contents should stay on one page (chapter 1 on page 4, as the
  build's last line says); look at changed pages with `pdfcraft-cli render <pdf> --page N --out x.png`.

Avoid `dotted`/`dashed` borders and CSS gradients: Chromium draws them as thousands of tiny
untagged shapes on every page (larger file, accessibility check fails). Use solid lines and SVG.
