"""Terminal album-cover rendering.

Draws images as ANSI half-block characters (U+2580) with 24-bit colors so
covers work in virtually every modern terminal without extra tools.
Rendered covers are cached in memory per (key, size) and source bytes are
cached on disk for remote artwork.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Callable

from rich.style import Style
from rich.text import Text

from .config import COVER_CACHE_DIR, ensure_cache_dirs

HALF_BLOCK = "▀"

_memory_cache: dict[tuple[str, int, int], Text] = {}


def _cache_file(key: str) -> Path:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return COVER_CACHE_DIR / f"{digest}.img"


def load_source_bytes(
    key: str,
    loader: Callable[[], bytes | None],
) -> bytes | None:
    """Return image bytes for a key, using the disk cache when possible."""
    ensure_cache_dirs()
    cache_path = _cache_file(key)
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        try:
            return cache_path.read_bytes()
        except OSError:
            pass
    data = loader()
    if data:
        try:
            cache_path.write_bytes(data)
        except OSError:
            pass
    return data


def clear_memory_cache() -> None:
    _memory_cache.clear()


def render_cover(data: bytes | None, cols: int, rows: int, key: str = "") -> Text:
    if key:
        cached = _memory_cache.get((key, cols, rows))
        if cached is not None:
            return cached
    text = _render_impl(data, cols, rows)
    if key:
        _memory_cache[(key, cols, rows)] = text
    return text


def placeholder(cols: int, rows: int) -> Text:
    return _render_impl(None, cols, rows)


def _render_impl(data: bytes | None, cols: int, rows: int) -> Text:
    bg_style = Style(color="rgb(70,70,80)", bgcolor="rgb(24,24,30)")
    if data is None:
        text = Text()
        for row in range(rows):
            if rows >= 3 and row == rows // 2:
                pad = max((cols - 1) // 2, 0)
                text.append(" " * pad, style=bg_style)
                text.append("♪", style=bg_style)
                text.append(" " * (cols - pad - 1), style=bg_style)
            else:
                text.append(" " * cols, style=bg_style)
            if row < rows - 1:
                text.append("\n")
        return text

    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return _render_impl(None, cols, rows)

    try:
        image = Image.open(io.BytesIO(data))
        image = image.convert("RGB")
    except Exception:
        return _render_impl(None, cols, rows)

    target_w = max(cols, 1)
    target_h = max(rows * 2, 2)
    ratio = min(target_w / image.width, target_h / image.height)
    new_w = max(1, round(image.width * ratio))
    new_h = max(1, round(image.height * ratio))
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    if ratio < 0.6:
        resized = resized.filter(
            ImageFilter.UnsharpMask(radius=1.4, percent=65, threshold=2)
        )

    canvas = Image.new("RGB", (target_w, target_h), (24, 24, 30))
    canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    pixels = canvas.load()

    text = Text(no_wrap=True, overflow="crop")
    for row in range(rows):
        run_fg: tuple[int, int, int] | None = None
        run_bg: tuple[int, int, int] | None = None
        run_len = 0

        def flush() -> None:
            nonlocal run_len
            if run_len <= 0 or run_fg is None or run_bg is None:
                return
            style = Style(
                color=f"rgb({run_fg[0]},{run_fg[1]},{run_fg[2]})",
                bgcolor=f"rgb({run_bg[0]},{run_bg[1]},{run_bg[2]})",
            )
            text.append(HALF_BLOCK * run_len, style=style)

        for col in range(target_w):
            fg = pixels[col, row * 2]
            bg = pixels[col, row * 2 + 1]
            if fg == run_fg and bg == run_bg:
                run_len += 1
            else:
                flush()
                run_fg, run_bg, run_len = fg, bg, 1
        flush()
        if row < rows - 1:
            text.append("\n")
    return text
