"""Entry point: `python -m comicsqueeze` (TUI) or with args (headless).

Windows uses spawn for multiprocessing, so the real work must sit behind a
`__main__` guard - otherwise every worker re-imports and re-runs this module.
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path


def _console_is_unicode() -> bool:
    """Whether the *original* console encoding can render box-drawing glyphs.

    Must be sampled before _prepare_stdio() switches the stream to UTF-8,
    otherwise it always reports true and a cp936/GBK console shows mojibake.
    """
    enc = (getattr(sys.stdout, "encoding", None) or "ascii").lower()
    return enc.replace("-", "") in ("utf8", "utf16", "utf32", "cp65001")


_UNICODE_OK = _console_is_unicode()


def _prepare_stdio() -> None:
    """Keep output printable on non-UTF-8 consoles (e.g. cp936/GBK on Windows).

    Without errors="replace", an unencodable character anywhere in a filename
    would abort the whole run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _ratio(text: str) -> float:
    raw = text.strip()
    try:
        val = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}")
    if val > 1.0:
        val /= 100  # "40" means 40%
    if not 0.01 <= val <= 1.0:
        raise argparse.ArgumentTypeError("ratio must be between 0.01 and 1.0")
    return val


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="comicsqueeze",
        description="Recompress every page image in comic PDFs to hit a target "
        "size ratio (out/in), then rebuild them as new PDFs. "
        "Run with no arguments for the terminal UI.",
    )
    p.add_argument(
        "-r", "--ratio", type=_ratio, default=0.5,
        help="target ratio, e.g. 0.4 or 40%% (default: 0.5)",
    )
    p.add_argument(
        "-d", "--dir", type=Path, default=None,
        help="folder to scan (default: the folder this tool lives in)",
    )
    p.add_argument(
        "-o", "--outdir", type=Path, default=None,
        help="where to write output (default: <dir>/compressed)",
    )
    p.add_argument(
        "-s", "--suffix", default="_compressed",
        help="appended to each output filename (default: _compressed)",
    )
    p.add_argument(
        "--no-ui", action="store_true",
        help="run headless with plain text progress instead of the TUI",
    )
    p.add_argument(
        "-y", "--yes", action="store_true",
        help="headless: skip the confirmation prompt",
    )
    return p


def run_headless(args, base: Path) -> int:
    from . import engine

    paths = engine.find_pdfs(base)
    if not paths:
        print(f"No PDFs found in {base}")
        return 1

    outdir = args.outdir or base / "compressed"
    if not outdir.is_absolute():
        outdir = base / outdir

    plans = []
    print(f"Scanning {len(paths)} PDF(s) in {base} ...")
    for path in paths:
        try:
            plan = engine.inspect(path)
        except Exception as exc:
            print(f"  ! {path.name}: unreadable ({exc})")
            continue
        if not plan.images:
            print(f"  - {path.name}: no page images, skipping")
            continue
        plans.append(plan)
        extra = f", {len(plan.passthrough)} page(s) copied as-is" if plan.passthrough else ""
        print(f"  {path.name}: {plan.page_count} pages, {engine.human(plan.file_bytes)}{extra}")

    if not plans:
        print("Nothing to do.")
        return 1

    total_in = sum(p.file_bytes for p in plans)
    print(
        f"\n{len(plans)} file(s), {engine.human(total_in)} total -> target ratio "
        f"{args.ratio:.2f} (~{engine.human(total_in * args.ratio)})"
    )
    print(f"Output: {outdir}")
    if not args.yes:
        try:
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 1
        except EOFError:
            print("Aborted (no input available; pass -y to skip this prompt).")
            return 1

    outdir.mkdir(parents=True, exist_ok=True)
    done_in = done_out = 0
    failures = 0

    for n, plan in enumerate(plans, 1):
        dst = outdir / f"{plan.path.stem}{args.suffix}{plan.path.suffix}"
        if dst.resolve() == plan.path.resolve():
            print(f"({n}/{len(plans)}) {plan.path.name}: would overwrite source, skipped")
            failures += 1
            continue

        print(f"\n({n}/{len(plans)}) {plan.path.name}")
        try:
            cal = engine.calibrate(plan, args.ratio)
        except Exception as exc:
            print(f"  calibration failed: {exc}")
            failures += 1
            continue
        note = "" if cal.exact else " (closest reachable)"
        print(f"  settings {cal.params.label()} -> predicted {cal.predicted_ratio:.3f}{note}")

        width = 34
        fill, empty_ch = ("█", "·") if _UNICODE_OK else ("#", "-")
        def on_page(done: int, total: int) -> None:
            filled = round(width * done / total)
            pct = 100 * done / total
            try:
                sys.stdout.write(
                    f"\r  [{fill * filled}{empty_ch * (width - filled)}] "
                    f"{pct:5.1f}%  {done}/{total}"
                )
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                # Output is being piped to something that stopped reading
                # (e.g. `| head`); keep converting rather than dying.
                pass

        try:
            res = engine.compress(plan, cal.params, dst, on_page=on_page)
        except Exception as exc:
            print(f"\n  failed: {exc}")
            failures += 1
            continue
        if res is None:
            print("\n  cancelled")
            break

        done_in += res.in_bytes
        done_out += res.out_bytes
        print(
            f"\r  {engine.human(res.in_bytes)} -> {engine.human(res.out_bytes)}"
            f" (ratio {res.ratio:.3f}) -> {dst.name}".ljust(width + 40)
        )

    if done_in:
        print(
            f"\nTotal: {engine.human(done_in)} -> {engine.human(done_out)} "
            f"(saved {engine.human(done_in - done_out)}, overall ratio {done_out / done_in:.3f})"
        )
    if failures:
        print(f"{failures} file(s) did not complete.")
    return 1 if failures and not done_in else 0


def main(argv: list[str] | None = None) -> int:
    _prepare_stdio()
    args = build_parser().parse_args(argv)
    # Default to the tool's own folder so double-clicking works as expected.
    base = (args.dir or Path(__file__).resolve().parent.parent).resolve()
    if not base.is_dir():
        print(f"Not a folder: {base}")
        return 2

    if args.no_ui:
        return run_headless(args, base)

    from .ui import SqueezeApp

    SqueezeApp(base, args.ratio).run()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
