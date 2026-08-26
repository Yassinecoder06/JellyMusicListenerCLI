"""Tests for the jmlcli command-line interface."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from music_listener import cli  # noqa: E402
from music_listener import config as cfg  # noqa: E402


def make_tone(path: Path, seconds: int = 1, freq: int = 440) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            str(path),
        ],
        check=True,
    )


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg.CONFIG_DIR = Path(self.tmp.name) / "config"
        cfg.CONFIG_PATH = cfg.CONFIG_DIR / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        return cli.main(list(argv))


class BasicCLITests(CLIBase):
    def test_version(self):
        self.assertEqual(self.run_cli("--version"), 0)

    def test_no_command_launches_tui_namespace(self):
        parser = cli.build_parser()
        args = parser.parse_args([])
        self.assertIsNone(getattr(args, "func", None))

    def test_source_get_and_set(self):
        out = self.run_cli("source")
        self.assertEqual(out, 0)
        self.assertEqual(cfg.load_config().active_source, "jellyfin")
        self.assertEqual(self.run_cli("source", "local"), 0)
        self.assertEqual(cfg.load_config().active_source, "local")

    def test_setup_local_folder(self):
        folder = Path(self.tmp.name) / "music"
        (folder / "Artist" / "Album (2020)").mkdir(parents=True)
        make_tone(folder / "Artist" / "Album (2020)" / "01 - Song.mp3")
        code = self.run_cli("setup", "--folder", str(folder), "--source", "local")
        self.assertEqual(code, 0)
        saved = cfg.load_config()
        self.assertEqual(saved.music_folder, str(folder))
        self.assertEqual(saved.active_source, "local")

    def test_setup_bad_folder_fails(self):
        code = self.run_cli("setup", "--folder", "/definitely/not/here")
        self.assertEqual(code, 1)


class LocalLibraryCLITests(CLIBase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.tmp.name) / "music"
        album = self.folder / "Neon" / "Drive (2021)"
        album.mkdir(parents=True)
        make_tone(album / "01 - Nightcall.mp3")
        make_tone(album / "02 - Sunrise.wav", freq=550)
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg.save_config(cfg.AppConfig(music_folder=str(self.folder), active_source="local"))

    def test_search_local(self):
        code = self.run_cli("search", "nightcall", "--local")
        self.assertEqual(code, 0)

    def test_library_albums_local(self):
        code = self.run_cli("library", "albums", "--local")
        self.assertEqual(code, 0)

    def test_play_dry_run_resolution(self):
        tracks = cli._local_filter(cli._local_snapshot(cfg.load_config()), "sunrise")
        self.assertEqual([t.title for t in tracks], ["Sunrise"])


if __name__ == "__main__":
    unittest.main()
