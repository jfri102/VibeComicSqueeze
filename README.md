# ComicSqueeze

Recompress the page images inside scanned comic/manga PDFs and rebuild them as
new files, targeting a **size ratio** you choose:

```
ratio = compressed size / original size
```

Only the top-level folder is scanned — subfolders are ignored, and source
files are never modified or overwritten. Ships with a terminal UI (Textual)
and a scriptable headless mode.

```
2.74 GB (14 volumes) --ratio 0.45--> 1.27 GB, all pages verified
```

## Why calibration instead of a fixed quality setting

Comic scans are usually already JPEG. Lowering JPEG quality alone bottoms out
quickly — often around ratio 0.35–0.40 — and pushing quality lower than that
just adds artifacts without shrinking the file further. So instead of a fixed
`quality=N`, ComicSqueeze **searches** for the settings that hit your target:

1. Sample ~12 pages spread across the book.
2. Binary-search JPEG quality at native resolution — the cheaper, less
   destructive knob.
3. Only if the target is still unreachable does it start downscaling pixels,
   refining the scale factor by secant iteration.
4. Apply the winning `(quality, scale)` to every page.

Calibration takes a few seconds per file and its cost doesn't grow with page
count. A ratio near 1.0 skips re-encoding entirely and copies the original
image bytes, so you never pay generation loss for nothing.

Because the target is a *size* ratio, an aggressive one (below ~0.3) means
real, visible quality loss — screentones soften once downscaling kicks in.
Above ~0.4, pages typically stay at native resolution and the change is
mostly invisible. The UI shows you which regime a given ratio falls into as
you type it.

## Install

Requires **Python 3.11+**.

```bash
git clone <this-repo>
cd comicsqueeze
pip install -r requirements.txt
```

Or directly:

```bash
pip install PyMuPDF Pillow textual
```

Dependencies: [PyMuPDF](https://pymupdf.readthedocs.io/) (PDF image
read/write), [Pillow](https://pillow.readthedocs.io/) (JPEG re-encoding),
[Textual](https://textual.textualize.io/) (terminal UI).

> **Windows + MSYS2/MinGW users:** if `python`/`pip` resolves to an MSYS2
> install, `pip install` will refuse with `externally-managed-environment`
> (PEP 668). Use a venv instead:
> ```powershell
> python -m venv .venv
> .\.venv\Scripts\pip install PyMuPDF Pillow textual
> .\.venv\Scripts\python -m comicsqueeze
> ```

## Run

Place `comicsqueeze/` in the folder containing your comic PDFs (or point `-d`
at that folder), then:

```bash
python -m comicsqueeze              # terminal UI
```

On Windows, `squeeze.bat` in the repo root does the same and can be
double-clicked.

### Terminal UI

- Enter a target ratio (`0.4`, `40%`, or `40` all mean 40%).
- `Space` toggles the file under the cursor; `a` selects/deselects all.
- `Ctrl+R` (or `F5`) starts the batch; `Ctrl+X` cancels after the current page.
- `Ctrl+Q` (or `F10`, or the Quit button) exits — cancels any in-progress
  write first, so no partial file is left behind.
- `Tab`/`Shift+Tab` move between fields; `Ctrl+N`/`Ctrl+P` and `Esc` are
  fallbacks for terminals that don't forward Shift+Tab or Ctrl+Q correctly.

### Headless / scripting

```bash
python -m comicsqueeze --no-ui -r 0.35 -y
python -m comicsqueeze --no-ui -r 40% -d "/path/to/comics" -o "/path/to/out" -s "_small"
```

| Flag | Meaning | Default |
|---|---|---|
| `-r`, `--ratio` | target ratio: `0.35`, `35%`, or `35` | `0.5` |
| `-d`, `--dir` | folder to scan | folder containing this tool |
| `-o`, `--outdir` | output folder | `<dir>/compressed` |
| `-s`, `--suffix` | appended to each output filename | `_compressed` |
| `--no-ui` | headless mode, plain-text progress | off |
| `-y`, `--yes` | skip the headless confirmation prompt | off |

## What gets touched

- A page is recompressed only if it is a single, full-page, unmasked image —
  the common case for scanned comics. Anything else (text pages, multi-image
  layouts, transparency) is copied through verbatim, so nothing is silently
  dropped or corrupted.
- Document title/author metadata and bookmarks (TOC) are preserved.
- Pages are encoded in parallel across CPU cores. Output is written to a
  `.part` file and atomically renamed on success, so a cancel or crash never
  leaves a half-written PDF.
- A corrupt or unreadable PDF is reported and skipped; the rest of the batch
  continues.
- Output never overwrites a source file — if the computed destination path
  would collide with the source, that file is skipped with a warning.

## License

MIT.
