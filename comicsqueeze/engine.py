"""Core compression engine: sample-calibrated, ratio-targeting PDF image recompressor.

The source PDFs are already-compressed JPEGs, so quality reduction alone bottoms
out around ratio 0.34. To hit an arbitrary user-supplied target we search a
2-D parameter space (JPEG quality x downscale factor) against a page sample,
then apply the winning parameters to the whole document.
"""

from __future__ import annotations

import io
import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image, ImageFile

# Comic scans are sometimes slightly truncated; salvage them instead of dying.
ImageFile.LOAD_TRUNCATED_IMAGES = True
# These scans are legitimately huge (1829x2600+). The default bomb guard exists
# for untrusted input; we are pointed at the user's own local library.
Image.MAX_IMAGE_PIXELS = None

QUALITY_FLOOR = 20
QUALITY_CEIL = 95
SCALE_FLOOR = 0.20
CHUNK_PAGES = 24


@dataclass(frozen=True)
class Params:
    """One point in the search space.

    `passthrough` means "reuse the original JPEG bytes" — the right answer when
    the requested ratio is so close to 1.0 that any re-encode would throw away
    quality for nothing.
    """

    quality: int
    scale: float
    passthrough: bool = False

    def label(self) -> str:
        if self.passthrough:
            return "copy images as-is"
        if self.scale > 0.999:
            return f"q{self.quality}"
        return f"q{self.quality} @ {self.scale * 100:.0f}%"


@dataclass
class PageImage:
    index: int
    xref: int
    width: int
    height: int
    nbytes: int


@dataclass
class PdfPlan:
    """Everything known about a source file before any pixels are decoded."""

    path: Path
    page_count: int
    images: list[PageImage]
    image_bytes: int
    file_bytes: int
    passthrough: list[int] = field(default_factory=list)

    @property
    def image_share(self) -> float:
        return self.image_bytes / self.file_bytes if self.file_bytes else 0.0


def _covers_page(bbox, rect, tol: float = 0.02) -> bool:
    """True if the image bbox spans essentially the whole page."""
    try:
        bw, bh = abs(bbox[2] - bbox[0]), abs(bbox[3] - bbox[1])
    except Exception:
        return False
    if rect.width <= 0 or rect.height <= 0:
        return False
    return (
        bw >= rect.width * (1 - tol)
        and bh >= rect.height * (1 - tol)
        and bw <= rect.width * (1 + tol)
        and bh <= rect.height * (1 + tol)
    )


def inspect(path: Path) -> PdfPlan:
    """Read structure without decoding pixels.

    A page qualifies for recompression only if it is a single, opaque,
    full-page image. Anything else is copied through untouched so we never
    silently drop text, overlays, or transparency masks.
    """
    with pymupdf.open(path) as doc:
        images: list[PageImage] = []
        passthrough: list[int] = []
        total = 0
        for pno in range(doc.page_count):
            page = doc[pno]
            infos = page.get_images(full=True)
            if len(infos) != 1:
                passthrough.append(pno)
                continue
            try:
                placements = page.get_image_info()
            except Exception:
                placements = []
            if len(placements) != 1 or placements[0].get("has-mask"):
                passthrough.append(pno)
                continue
            if not _covers_page(placements[0].get("bbox"), page.rect):
                passthrough.append(pno)
                continue
            xref = infos[0][0]
            try:
                meta = doc.extract_image(xref)
            except Exception:
                passthrough.append(pno)
                continue
            if not meta.get("image"):
                passthrough.append(pno)
                continue
            total += len(meta["image"])
            images.append(
                PageImage(pno, xref, meta["width"], meta["height"], len(meta["image"]))
            )
        return PdfPlan(
            path=path,
            page_count=doc.page_count,
            images=images,
            image_bytes=total,
            file_bytes=path.stat().st_size,
            passthrough=passthrough,
        )


def encode(data: bytes, params: Params) -> bytes:
    """Recompress one JPEG. Keeps the original bytes if recompression inflates."""
    if params.passthrough:
        return data
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.mode == "1":
            im = im.convert("L")
        elif im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        if params.scale < 0.999:
            w = max(1, round(im.width * params.scale))
            h = max(1, round(im.height * params.scale))
            im = im.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=params.quality, optimize=True, progressive=True)
    out = buf.getvalue()
    if len(out) >= len(data) and params.scale > 0.999:
        return data
    return out


def _sample_indices(count: int, want: int) -> list[int]:
    """Evenly spaced sample across the document, always including the cover."""
    if count <= want:
        return list(range(count))
    step = count / want
    idx = sorted({min(count - 1, int(i * step)) for i in range(want)})
    idx[0] = 0
    return idx


# --- worker side --------------------------------------------------------------
# The sample is shipped once per worker at pool startup, so each probe only
# sends (index, quality, scale) rather than megabytes of JPEG.
_SAMPLE: list[bytes] = []


def _init_sample(sample: list[bytes]) -> None:
    global _SAMPLE
    _SAMPLE = sample


def _size_of(args: tuple[int, int, float]) -> int:
    idx, quality, scale = args
    return len(encode(_SAMPLE[idx], Params(quality, scale)))


def _encode_one(args: tuple[int, bytes, int, float, bool]) -> tuple[int, bytes]:
    idx, data, quality, scale, passthrough = args
    return idx, encode(data, Params(quality, scale, passthrough))


def _pool_size() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


@dataclass
class Calibration:
    params: Params
    predicted_ratio: float
    probes: int
    exact: bool


def calibrate(
    plan: PdfPlan,
    target: float,
    sample_size: int = 12,
    tolerance: float = 0.02,
    max_probes: int = 16,
    progress: Callable[[str], None] | None = None,
) -> Calibration:
    """Search (quality, scale) for the combination closest to `target` ratio.

    Quality is spent first and downscaling only once quality bottoms out, since
    resampling is the more destructive knob. Cost is set by the sample size, not
    the page count, so calibration stays cheap on a 200-page book.
    """
    if not plan.images:
        return Calibration(Params(QUALITY_CEIL, 1.0, passthrough=True), 1.0, 0, False)

    with pymupdf.open(plan.path) as doc:
        picks = _sample_indices(len(plan.images), sample_size)
        sample = [doc.extract_image(plan.images[i].xref)["image"] for i in picks]

    base = sum(len(b) for b in sample)
    if base == 0:
        return Calibration(Params(QUALITY_CEIL, 1.0, passthrough=True), 1.0, 0, False)

    def note(msg: str) -> None:
        if progress:
            progress(msg)

    cache: dict[Params, float] = {}
    probes = 0
    workers = min(_pool_size(), len(sample))

    # Reusing the source bytes is always exactly ratio 1.0 and costs no quality,
    # so it is the baseline every re-encode has to beat.
    best = Params(QUALITY_CEIL, 1.0, passthrough=True)
    best_r = 1.0
    if target >= 1.0 - tolerance:
        note("target is ~1.0 - copying images untouched")
        return Calibration(best, 1.0, 0, True)

    with ProcessPoolExecutor(
        max_workers=workers, initializer=_init_sample, initargs=(sample,)
    ) as pool:

        def ratio_of(p: Params) -> float:
            """Measure one candidate, fanning the sample pages across workers."""
            nonlocal probes
            if p in cache:
                return cache[p]
            probes += 1
            jobs = [(i, p.quality, p.scale) for i in range(len(sample))]
            total = sum(pool.map(_size_of, jobs, chunksize=1))
            r = total / base
            cache[p] = r
            note(f"probe {p.label()} -> {r:.3f}")
            return r

        ceil_p = Params(QUALITY_CEIL, 1.0)
        ceil_r = ratio_of(ceil_p)
        if abs(ceil_r - target) < abs(best_r - target):
            best, best_r = ceil_p, ceil_r

        # Phase 1: binary search on quality at native resolution.
        lo, hi = QUALITY_FLOOR, QUALITY_CEIL
        while lo <= hi and probes < max_probes:
            mid = (lo + hi) // 2
            p = Params(mid, 1.0)
            r = ratio_of(p)
            if abs(r - target) < abs(best_r - target):
                best, best_r = p, r
            if abs(r - target) <= tolerance:
                return Calibration(p, r, probes, True)
            if r > target:
                hi = mid - 1
            else:
                lo = mid + 1

        floor_r = ratio_of(Params(QUALITY_FLOOR, 1.0))
        if abs(floor_r - target) < abs(best_r - target):
            best, best_r = Params(QUALITY_FLOOR, 1.0), floor_r

        # Reachable by quality alone.
        if target >= floor_r - tolerance:
            return Calibration(best, best_r, probes, abs(best_r - target) <= tolerance)

        # Phase 2: below the quality floor, so start shrinking pixels. Bytes track
        # pixel count, so seed from sqrt(area ratio) then refine by secant.
        note("quality floor reached - engaging downscale")
        q = 62
        anchor = ratio_of(Params(q, 1.0))
        seed = math.sqrt(max(0.01, min(1.0, target / max(anchor, 1e-6))))
        scale = max(SCALE_FLOOR, min(1.0, seed))

        prev_scale, prev_r = 1.0, anchor
        p = Params(q, scale)
        r = ratio_of(p)
        if abs(r - target) < abs(best_r - target):
            best, best_r = p, r

        while probes < max_probes and abs(r - target) > tolerance:
            denom = r - prev_r
            if abs(denom) < 1e-9:
                break
            nxt = scale + (target - r) * (scale - prev_scale) / denom
            nxt = max(SCALE_FLOOR, min(1.0, nxt))
            if abs(nxt - scale) < 0.004:
                break
            prev_scale, prev_r = scale, r
            scale = nxt
            p = Params(q, scale)
            r = ratio_of(p)
            if abs(r - target) < abs(best_r - target):
                best, best_r = p, r

    return Calibration(best, best_r, probes, abs(best_r - target) <= tolerance)


@dataclass
class Result:
    src: Path
    dst: Path
    in_bytes: int
    out_bytes: int
    params: Params
    pages: int
    passthrough: int

    @property
    def ratio(self) -> float:
        return self.out_bytes / self.in_bytes if self.in_bytes else 1.0


def compress(
    plan: PdfPlan,
    params: Params,
    dst: Path,
    on_page: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Result | None:
    """Rebuild the PDF with recompressed images. Returns None if cancelled.

    A fresh document is built rather than mutating in place: PyMuPDF's
    `replace_image` leaves the original streams reachable, so the output comes
    out the same size as the input (measured: 169 MB in, 169 MB out, plus xref
    errors). Pages are encoded in parallel chunks to keep peak memory bounded.
    """
    tmp = dst.with_suffix(dst.suffix + ".part")
    by_page = {img.index: img for img in plan.images}
    workers = min(_pool_size(), max(1, len(plan.images)))
    src_doc = pymupdf.open(plan.path)
    out_doc = pymupdf.open()
    # One pool for the whole file: on Windows each spawn costs real time, so
    # re-creating it per chunk would dominate the runtime.
    pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    cancelled = False
    try:
        done = 0
        for start in range(0, plan.page_count, CHUNK_PAGES):
            if should_stop and should_stop():
                cancelled = True
                return None
            stop = min(start + CHUNK_PAGES, plan.page_count)
            batch = [
                (pno, src_doc.extract_image(by_page[pno].xref)["image"])
                for pno in range(start, stop)
                if pno in by_page
            ]
            encoded: dict[int, bytes] = {}
            if batch:
                if pool is not None:
                    jobs = [
                        (pno, data, params.quality, params.scale, params.passthrough)
                        for pno, data in batch
                    ]
                    for pno, blob in pool.map(_encode_one, jobs, chunksize=1):
                        encoded[pno] = blob
                else:
                    for pno, data in batch:
                        encoded[pno] = encode(data, params)

            for pno in range(start, stop):
                if should_stop and should_stop():
                    cancelled = True
                    return None
                rect = src_doc[pno].rect
                npage = out_doc.new_page(width=rect.width, height=rect.height)
                if pno in encoded:
                    npage.insert_image(npage.rect, stream=encoded[pno])
                else:
                    # Not a plain full-page image: copy the page verbatim.
                    npage.show_pdf_page(npage.rect, src_doc, pno)
                done += 1
                if on_page:
                    on_page(done, plan.page_count)

        out_doc.set_metadata(src_doc.metadata or {})
        toc = src_doc.get_toc()
        if toc:
            out_doc.set_toc(toc)
        out_doc.save(tmp, garbage=4, deflate=True, clean=True)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        out_doc.close()
        src_doc.close()
        if cancelled:
            tmp.unlink(missing_ok=True)

    os.replace(tmp, dst)
    return Result(
        src=plan.path,
        dst=dst,
        in_bytes=plan.file_bytes,
        out_bytes=dst.stat().st_size,
        params=params,
        pages=plan.page_count,
        passthrough=len(plan.passthrough),
    )


def find_pdfs(directory: Path) -> list[Path]:
    """Top-level PDFs only - subfolders are deliberately ignored."""
    return sorted(
        (
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("_")
        ),
        key=lambda p: p.name.lower(),
    )


def human(n: float) -> str:
    if abs(n) < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.2f} {unit}"
    return f"{n:.2f} GB"
