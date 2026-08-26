"""Bottom now-playing bar: cover, metadata, progress and flags."""

from __future__ import annotations

from rich.text import Text

from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from ..models import Track, fmt_duration
from .cover import CoverWidget

BAR_WIDTH = 24

STATE_ICONS = {"playing": "▶", "paused": "⏸", "stopped": "■"}


def progress_line(state: str, position: float | None, duration: float | None) -> str:
    icon = STATE_ICONS.get(state, "•")
    elapsed = fmt_duration(position)
    if not duration or duration <= 0:
        return f"{icon} {elapsed}"
    ratio = max(0.0, min(1.0, (position or 0) / duration))
    filled = int(round(ratio * BAR_WIDTH))
    if filled >= BAR_WIDTH:
        bar = "━" * BAR_WIDTH
    else:
        bar = ("━" * filled) + "●" + ("─" * (BAR_WIDTH - filled - 1))
    total = fmt_duration(duration)
    return f"{icon} {elapsed} {bar} {total}"


class NowPlayingBar(Horizontal):
    def __init__(self, cover_cols: int = 40, cover_rows: int = 20, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track: Track | None = None
        self._cover_geometry = (cover_cols, cover_rows)

    def compose(self):
        yield CoverWidget(*self._cover_geometry, id="np-cover")
        with Vertical(id="np-info"):
            yield Static("Nothing playing", id="np-title")
            yield Static("", id="np-sub")
            yield Static(progress_line("stopped", None, None), id="np-progress", markup=False)
        with Vertical(id="np-side"):
            yield Static("", id="np-source")
            yield Static("", id="np-flags")

    def _q(self, selector: str):
        found = self.query(selector)
        return found.first() if found else None

    def set_track(self, track: Track | None) -> None:
        self._track = track
        title = self._q("#np-title")
        sub = self._q("#np-sub")
        if title is None or sub is None:
            return
        if track is None:
            title.update("Nothing playing")
            sub.update("")
            return
        title.update(Text(track.title, style="bold"))
        parts = [p for p in (track.artist, track.album) if p]
        year = f"· {track.year}" if track.year else ""
        sub.update(f"{' · '.join(parts)} {year}".strip())

    def update_status(self, state: str, position: float | None, duration: float | None) -> None:
        progress = self._q("#np-progress")
        if progress is None:
            return
        effective = duration if (duration and duration > 0) else (self._track.duration if self._track else None)
        progress.update(progress_line(state, position, effective))

    def set_cover_data(self, data: bytes | None) -> None:
        cover = self._q("#np-cover")
        if cover is not None:
            cover.set_image(data)

    def set_source(self, label: str) -> None:
        node = self._q("#np-source")
        if node is not None:
            node.update(label)

    def set_flags(self, volume: float | None, muted: bool | None, shuffle: bool, repeat: str) -> None:
        flags = self._q("#np-flags")
        if flags is None:
            return
        vol = "-" if muted else f"{int(volume)}%" if volume is not None else "?"
        repeat_flag = {"off": "", "all": "⟳all", "one": "⟳1"}.get(repeat, "")
        shuffle_flag = "🔀" if shuffle else ""
        flags.update(f"vol {vol}  {shuffle_flag}{repeat_flag}")
