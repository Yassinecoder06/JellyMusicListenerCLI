"""Headless TUI tests driven by Textual's pilot."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from music_listener import config as cfg  # noqa: E402
from music_listener.app import (  # noqa: E402
    LEVEL_ALBUMS,
    LEVEL_ARTISTS,
    LEVEL_PLAYLISTS,
    LEVEL_TRACKS,
    ListenerApp,
)
from music_listener.models import Track  # noqa: E402


def make_tone(path: Path, seconds: int = 1, freq: int = 440) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            str(path),
        ],
        check=True,
    )


class TUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "music"
        album_dir = root / "TUI Artist" / "First Album (2024)"
        album_dir.mkdir(parents=True)
        make_tone(album_dir / "01 - Alpha.mp3")
        make_tone(album_dir / "02 - Beta.mp3", freq=550)

        old_dir = cfg.CONFIG_DIR
        old_path = cfg.CONFIG_PATH
        cfg.CONFIG_DIR = Path(self.tmp.name) / "config"
        cfg.CONFIG_PATH = cfg.CONFIG_DIR / "config.json"
        self._old = (old_dir, old_path)

        self.app = ListenerApp(local_root=str(root))
        self.root = root

    def tearDown(self):
        cfg.CONFIG_DIR, cfg.CONFIG_PATH = self._old
        self.tmp.cleanup()

    async def test_local_browse_and_play(self):
        async with self.app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            for _ in range(30):
                await pilot.pause(0.1)
                if self.app.rows:
                    break
            self.assertTrue(self.app.rows, "album list never filled")
            self.assertEqual(len(self.app.rows), 1)
            album_row = self.app.rows[0]
            self.assertEqual(album_row.name, "First Album")

            self.app.push_view(LEVEL_TRACKS)
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.1)
                if len(self.app.rows) >= 2:
                    break
            self.assertEqual(len(self.app.rows), 2)

            table = self.app.query_one("#table")
            table.focus()
            await pilot.press("enter")
            await pilot.pause(0.5)
            self.assertIsNotNone(self.app.current_track)
            self.assertIsInstance(self.app.current_track, Track)
            status = self.app.player.status() if self.app.player else None
            if status is not None and status.state != "stopped":
                await pilot.press("space")
                self.assertEqual(self.app.player.status().state, "paused")
                await pilot.press("space")

            await pilot.press("x")
            await pilot.pause(0.2)
            self.assertTrue(0 <= self.app.q_pos < max(len(self.app.order), 1))

    async def test_search_flow(self):
        async with self.app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            for _ in range(30):
                await pilot.pause(0.1)
                if self.app.rows:
                    break
            await pilot.press("/")
            box = self.app.query_one("#searchbox")
            self.assertTrue(box.has_class("visible"))
            box.value = "beta"
            await pilot.press("enter")
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.1)
                if self.app.current_level == "search":
                    break
            self.assertEqual(self.app.current_level, "search")
            titles = [t.title for t in self.app.rows]
            self.assertEqual(titles, ["Beta"])

    async def test_artist_drilldown_and_back(self):
        async with self.app.run_test(size=(120, 38)) as pilot:
            await pilot.pause()
            for _ in range(30):
                await pilot.pause(0.1)
                if self.app.rows:
                    break
            self.app.push_view(LEVEL_ARTISTS)
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.1)
                if self.app.rows:
                    break
            self.assertGreaterEqual(len(self.app.rows), 1)
            artist = self.app.rows[0]
            self.assertEqual(artist.name, "TUI Artist")

            self.app.push_view("artist_detail", {"artist": artist})
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.1)
                if self.app.rows:
                    break
            self.assertEqual([a.name for a in self.app.rows], ["First Album"])

            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.app.stack[-1][0], LEVEL_ARTISTS)


if __name__ == "__main__":
    unittest.main()
