"""Cover-art widget.

Displays album covers at true resolution through the terminal's native
graphics protocol when available (Kitty graphics protocol on kitty,
ghostty, WezTerm; Sixel on foot/mlterm/mintty/xterm), falling back to a
half-block ANSI renderer everywhere else.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from .. import coverart


def _detect_graphics_mode() -> str:
    try:
        if not sys.stdout.isatty():
            return "fallback"
    except Exception:
        return "fallback"
    env = os.environ
    if (
        env.get("KITTY_WINDOW_ID")
        or env.get("GHOSTTY_RESOURCES_DIR")
        or env.get("GHOSTTY_BIN_DIR")
        or env.get("WEZTERM_EXECUTABLE")
    ):
        return "tgp"
    marker = (env.get("TERM", "") + " " + env.get("TERM_PROGRAM", "")).lower()
    if any(name in marker for name in ("sixel", "mlterm", "foot", "mintty")):
        return "sixel"
    return "fallback"


class CoverWidget(Vertical):
    def __init__(self, cols: int = 38, rows: int = 19, **kwargs) -> None:
        self._cols = cols
        self._rows = rows
        self._mode = _detect_graphics_mode()
        self._data: bytes | None = None
        self._data_key = ""
        self._fit_key = None
        self._fit_image = None
        super().__init__(**kwargs)

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def mode(self) -> str:
        return self._mode

    def compose(self):
        if self._mode == "tgp":
            from textual_image.widget import TGPImage

            yield TGPImage(id="np-cover-real")
        elif self._mode == "sixel":
            from textual_image.widget import SixelImage

            yield SixelImage(id="np-cover-real")
        else:
            yield Static("", id="np-cover-fallback", markup=False)

    def on_mount(self) -> None:
        self.apply_size(self._cols, self._rows)
        self._refresh_content()

    def apply_size(self, cols: int, rows: int) -> None:
        self._cols = cols
        self._rows = rows
        self.styles.width = cols
        self.styles.height = rows
        self._refresh_content()

    def set_image(self, data: bytes | None) -> None:
        self._data = data
        self._data_key = hashlib.md5(data).hexdigest() if data else ""
        self._fit_key = None
        self._fit_image = None
        self._refresh_content()

    def _terminal_cell_pixels(self) -> tuple[int, int]:
        try:
            from textual_image._terminal import get_cell_size

            cell = get_cell_size()
            return int(cell.width), int(cell.height)
        except Exception:
            return 10, 20

    def _fitted_image(self):
        """Scale the cover to exactly fill the widget box, pixel-perfect."""
        if self._data is None:
            return None
        cell_w, cell_h = self._terminal_cell_pixels()
        target_w = max(self._cols * cell_w, 1)
        target_h = max(self._rows * cell_h, 1)
        key = (self._data_key, target_w, target_h)
        if self._fit_key == key and self._fit_image is not None:
            return self._fit_image

        from PIL import ImageFilter, Image as PILImage

        image = PILImage.open(io.BytesIO(self._data)).convert("RGB")
        scale = max(target_w / image.width, target_h / image.height)
        new_w = max(1, round(image.width * scale))
        new_h = max(1, round(image.height * scale))
        resized = image.resize((new_w, new_h), PILImage.LANCZOS)
        if scale < 0.6:
            resized = resized.filter(
                ImageFilter.UnsharpMask(radius=1.2, percent=55, threshold=2)
            )
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        fitted = resized.crop((left, top, left + target_w, top + target_h))
        self._fit_key = key
        self._fit_image = fitted
        return fitted

    def _refresh_content(self) -> None:
        if self._mode == "fallback":
            finder = self.query("#np-cover-fallback")
            if not finder:
                return
            key = ""
            if self._data:
                key = self._data_key
                rendered = coverart.render_cover(
                    self._data, self._cols, self._rows, key=key
                )
            else:
                rendered = coverart.placeholder(self._cols, self._rows)
            finder.first().update(rendered)
            return

        real = self.query("#np-cover-real")
        if not real:
            return
        widget = real.first()
        fitted = self._fitted_image()
        if fitted is None:
            from PIL import Image as PILImage

            widget.image = PILImage.new("RGB", (16, 16), (24, 24, 30))
            return
        try:
            widget.image = fitted
        except Exception:
            pass

    def fallback_text(self) -> Text | None:
        if self._mode != "fallback":
            return None
        finder = self.query("#np-cover-fallback")
        if not finder:
            return None
        return getattr(finder.first(), "renderable", None)
