"""Smoke tests for the music listener package."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from music_listener import config as cfg  # noqa: E402
from music_listener import coverart, locallib, localplaylists  # noqa: E402
from music_listener.jellyfin import JellyfinClient, JellyfinError, connect  # noqa: E402
from music_listener.models import Album, Track, fmt_duration  # noqa: E402


def make_png(width: int = 40, height: int = 30, color=(200, 10, 10)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class MockJellyfin(BaseHTTPRequestHandler):
    token_store = {}
    items = {}
    last_auth_headers = {}

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/Users/AuthenticateByName"):
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            MockJellyfin.last_auth_headers = {
                k.lower(): v for k, v in self.headers.items()
            }
            if not self.headers.get("X-Emby-Authorization"):
                self._json({}, status=400)
                return
            if data.get("Username") != "tester" or data.get("Pw") != "secret":
                self._json({}, status=401)
                return
            self._json(
                {
                    "AccessToken": "tok123",
                    "User": {"Id": "user-1", "Name": "tester"},
                }
            )
            return
        self._json({})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/System/Info/Public":
            self._json({"ProductName": "Jellyfin", "Version": "10.9.0"})
            return
        auth_ok = self.headers.get("X-Emby-Token") == "tok123"
        if not auth_ok:
            self._json({}, status=401)
            return
        if path == "/Users/user-1":
            self._json({"Id": "user-1", "Name": "tester"})
            return
        if path == "/Users/user-1/Views":
            self._json({"Items": [{"Id": "music-1", "Name": "Music", "CollectionType": "music"}]})
            return
        if path == "/Items":
            params = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p)
            include = params.get("includeItemTypes", "")
            parent = params.get("parentId", "")
            if include == "MusicArtist":
                self._json({"Items": MockJellyfin.items["artists"], "TotalRecordCount": 1})
                return
            if include == "MusicAlbum":
                self._json({"Items": MockJellyfin.items["albums"], "TotalRecordCount": 1})
                return
            if include == "Playlist":
                self._json({"Items": MockJellyfin.items["playlists"], "TotalRecordCount": 1})
                return
            if include == "Audio" and parent == "album-9":
                self._json({"Items": MockJellyfin.items["album_tracks"], "TotalRecordCount": 1})
                return
            if include == "Audio":
                term = params.get("searchTerm", "")
                items = [
                    t for t in MockJellyfin.items["album_tracks"]
                    if term.lower() in t.get("Name", "").lower() or not term
                ]
                self._json({"Items": items, "TotalRecordCount": len(items)})
                return
        if path == "/Playlists/pl-1/Items":
            self._json({"Items": MockJellyfin.items["playlist_entries"]})
            return
        self._json({"Items": [], "TotalRecordCount": 0})

    def do_DELETE(self):
        self._json({})


class ConfigTests(unittest.TestCase):
    def test_roundtrip_in_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg.CONFIG_DIR = Path(tmp) / "cfg"
            cfg.CONFIG_PATH = cfg.CONFIG_DIR / "config.json"
            base = cfg.load_config()
            base.server_url = "http://x"
            cfg.save_config(base)
            loaded = cfg.load_config()
            self.assertEqual(loaded.server_url, "http://x")
            self.assertTrue(loaded.device_id)

    def test_keyring_failure_saves_and_loads_dotenv_password(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            old_dir, old_path = cfg.CONFIG_DIR, cfg.CONFIG_PATH
            cfg.CONFIG_DIR = Path(tmp) / "cfg"
            cfg.CONFIG_PATH = cfg.CONFIG_DIR / "config.json"
            try:
                with patch.dict(sys.modules, {"keyring": None}):
                    self.assertEqual(cfg.save_password("secret value"), "dotenv")
                self.assertEqual(cfg.get_password(), "secret value")
                self.assertEqual(cfg.dotenv_path().stat().st_mode & 0o777, 0o600)
            finally:
                cfg.CONFIG_DIR, cfg.CONFIG_PATH = old_dir, old_path

    def test_local_playlists_preserve_track_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_path = cfg.CONFIG_DIR, cfg.CONFIG_PATH
            cfg.CONFIG_DIR = Path(tmp) / "cfg"
            cfg.CONFIG_PATH = cfg.CONFIG_DIR / "config.json"
            tracks = [
                Track(id="/music/one.mp3", title="One", artist="Artist", album="Album", source="local"),
                Track(id="/music/two.mp3", title="Two", artist="Artist", album="Album", source="local"),
            ]
            try:
                playlist = localplaylists.create("Road Trip", [tracks[0].id])
                localplaylists.add_tracks(playlist.id, [tracks[1].id])
                listed = localplaylists.playlists(tracks)
                self.assertEqual((listed[0].name, listed[0].track_count), ("Road Trip", 2))
                entries = localplaylists.tracks(playlist.id, tracks)
                self.assertEqual((entries[0].title, entries[0].artist, entries[0].album), ("One", "Artist", "Album"))
                localplaylists.remove_tracks(playlist.id, [tracks[0].id])
                self.assertEqual([entry.id for entry in localplaylists.tracks(playlist.id, tracks)], [tracks[1].id])
                localplaylists.delete(playlist.id)
                self.assertEqual(localplaylists.playlists(tracks), [])
            finally:
                cfg.CONFIG_DIR, cfg.CONFIG_PATH = old_dir, old_path

    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(None), "-:--")
        self.assertEqual(fmt_duration(65), "1:05")
        self.assertEqual(fmt_duration(3671), "1:01:11")


class ModelTests(unittest.TestCase):
    def test_track_defaults(self):
        track = Track(id="1", title="T", artist="A")
        self.assertEqual(track.source, "jellyfin")
        self.assertEqual(track.duration_text, "-:--")

    def test_album_cover_key(self):
        album = Album(id="a", name="n", artist="ar", cover_key="k")
        self.assertEqual(album.cover_key, "k")


class CoverArtTests(unittest.TestCase):
    def test_render_real_image(self):
        text = coverart.render_cover(make_png(), 8, 4, key="test-img")
        plain = text.plain
        self.assertEqual(len(plain.splitlines()), 4)
        for line in plain.splitlines():
            self.assertLessEqual(len(line), 8)
        self.assertIn("▀", plain)

    def test_placeholder(self):
        text = coverart.placeholder(6, 3)
        self.assertIn("♪", text.plain)
        self.assertEqual(len(text.plain.splitlines()), 3)

    def test_corrupt_data_falls_back(self):
        text = coverart.render_cover(b"not-an-image", 6, 3)
        self.assertIn("♪", text.plain)


class LocalLibraryTests(unittest.TestCase):
    def _fixture(self, root: Path) -> None:
        album_dir = root / "Neon Nights" / "Midnight Drive (2021)"
        album_dir.mkdir(parents=True)
        (album_dir / "cover.jpg").write_bytes(make_png(color=(0, 128, 255)))
        tracks = ["01 - Nightcall.mp3", "02 - City Lights.mp3"]
        for i, name in enumerate(tracks, start=101000):
            self._make_tone(album_dir / name, seconds=1, freq=300 + i)
        loose_dir = root / "Solo Artist"
        loose_dir.mkdir(parents=True)
        self._make_tone(loose_dir / "Single Life.mp3", seconds=1, freq=520)

    @staticmethod
    def _make_tone(path: Path, seconds: int, freq: int) -> None:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            str(path),
        ]
        subprocess.run(cmd, check=True)

    def test_scan_finds_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            self._fixture(root)
            lib = locallib.LocalLibrary(str(root))
            snap = lib.scan()
            names = [a.name for a in snap.artists]
            self.assertIn("Neon Nights", names)
            self.assertIn("Solo Artist", names)
            albums = {(a.name, a.year, a.track_count) for a in snap.albums}
            self.assertIn(("Midnight Drive", 2021, 2), albums)
            titles = [t.title for t in snap.tracks]
            self.assertIn("Nightcall", titles)
            self.assertIn("City Lights", titles)
            single = next(t for t in snap.tracks if t.title == "Single Life")
            self.assertEqual(single.album, "[Singles]")
            durations = [t.duration for t in snap.tracks]
            self.assertTrue(all(d and d > 0.5 for d in durations))

    def test_find_cover_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "folder.jpg").write_bytes(b"x")
            (d / "cover.png").write_bytes(b"y")
            found = locallib.find_cover(d)
            self.assertEqual(found.name, "cover.png")


class JellyfinClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MockJellyfin.items = {
            "albums": [
                {
                    "Id": "album-9",
                    "Name": "Great Hits",
                    "ProductionYear": 2020,
                    "ChildCount": 2,
                    "ParentId": "artist-1",
                }
            ],
            "artists": [
                {"Id": "artist-1", "Name": "The Mocks", "ChildCount": 1}
            ],
            "album_tracks": [
                {
                    "Id": "tr-1",
                    "Name": "First Song",
                    "ParentId": "album-9",
                    "IndexNumber": 1,
                    "RunTimeTicks": 180_000_000 * 10,
                    "ProductionYear": 2020,
                },
                {
                    "Id": "tr-2",
                    "Name": "Second Song",
                    "ParentId": "album-9",
                    "IndexNumber": 2,
                    "RunTimeTicks": 200_000_000 * 10,
                },
            ],
            "playlists": [
                {"Id": "pl-1", "Name": "Road Trip", "ChildCount": 1}
            ],
            "playlist_entries": [
                {
                    "Id": "tr-1",
                    "Name": "First Song",
                    "ParentId": "album-9",
                    "RunTimeTicks": 180_000_000 * 10,
                    "PlaylistItemId": "entry-77",
                }
            ],
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockJellyfin)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_full_flow(self):
        client = connect(self.url, "tester", "secret", device_id="dev42")
        self.assertEqual(client.token, "tok123")
        self.assertIn("x-emby-authorization", MockJellyfin.last_auth_headers)
        self.assertIn("MediaBrowser", MockJellyfin.last_auth_headers["x-emby-authorization"])

        with self.assertRaises(JellyfinError) as wrong:
            connect(self.url, "tester", "nope", device_id="dev42")
        self.assertIn("401", str(wrong.exception))
        self.assertEqual(client.user_id, "user-1")

        artists = client.artists()
        self.assertEqual([artist.name for artist in artists], ["The Mocks"])

        albums = client.albums()
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0].artist, "The Mocks")
        self.assertEqual(albums[0].cover_key, "album-9")
        self.assertEqual(albums[0].year, 2020)

        artist_albums = client.albums(artist_id="artist-1")
        self.assertEqual([album.id for album in artist_albums], ["album-9"])

        tracks = client.album_tracks("album-9")
        self.assertEqual([t.title for t in tracks], ["First Song", "Second Song"])
        self.assertEqual((tracks[0].artist, tracks[0].album), ("The Mocks", "Great Hits"))
        self.assertAlmostEqual(tracks[0].duration, 180.0)

        fresh_client = connect(self.url, "tester", "secret", device_id="dev43")
        all_tracks = fresh_client.all_tracks()
        self.assertEqual((all_tracks[0].artist, all_tracks[0].album), ("The Mocks", "Great Hits"))

        url = client.stream_url("tr-1")
        self.assertIn("/Items/tr-1/Download", url)
        self.assertIn("api_key=tok123", url)
        img = client.image_url("album-9")
        self.assertIn("/Items/album-9/Images/Primary", img)

        found = client.search_tracks("second")
        self.assertEqual([t.id for t in found], ["tr-2"])

        playlists = client.playlists()
        self.assertEqual(playlists[0].name, "Road Trip")
        entries = client.playlist_tracks("pl-1")
        self.assertEqual(entries[0].playlist_entry_id, "entry-77")
        self.assertEqual((entries[0].title, entries[0].artist, entries[0].album), ("First Song", "The Mocks", "Great Hits"))

        new_id = client.create_playlist("Mix", ["tr-1"])
        self.assertEqual(new_id, "")
        client.add_to_playlist("pl-1", ["tr-2"])
        client.remove_from_playlist("pl-1", ["entry-77"])
        client.delete_playlist("pl-1")
        client.report_start("tr-1")
        client.report_progress("tr-1", 5.0)
        client.report_stop("tr-1", 9.0)


class PlayerTests(unittest.TestCase):
    TONE = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tone_path = Path(cls.tmp.name) / "tone.mp3"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-codec:a", "libmp3lame",
                str(cls.tone_path),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _player(self):
        from music_listener.player import MpvPlayer

        return MpvPlayer()

    def test_ssh_audio_target_uses_pipewire_or_pulse_device(self):
        from music_listener.player import _matching_audio_device

        devices = [
            {"name": "pipewire/SSH_Stream"},
            {"name": "pulse/SSH_Stream"},
            {"name": "pipewire/SSH_Output"},
        ]
        self.assertEqual(_matching_audio_device(devices, "SSH_Stream"), "pipewire/SSH_Stream")
        with patch.dict(os.environ, {"JMLCLI_SSH_AUDIO_DEVICE": "pipewire/SSH_Output"}):
            self.assertEqual(_matching_audio_device(devices, "SSH_Stream"), "pipewire/SSH_Output")
        self.assertIsNone(_matching_audio_device([], "SSH_Stream"))

    def test_play_pause_seek_volume_eof(self):
        try:
            player = self._player()
        except Exception as error:
            self.skipTest(f"no audio backend: {error}")
        ended = threading.Event()
        player.on_end_of_track = ended.set

        player.play(str(self.tone_path))
        time.sleep(1.0)
        status = player.status()
        self.assertIn(status.state, ("playing", "paused"))
        if status.state != "playing":
            self.skipTest("no audible output device available")
        self.assertGreater(status.position or 0, 0.1)

        player.set_paused(True)
        pos1 = player.status().position
        time.sleep(0.6)
        pos2 = player.status().position
        self.assertAlmostEqual(pos1 or 0, pos2 or 0, delta=0.35)

        player.seek(-100)
        time.sleep(0.4)
        self.assertLess(player.status().position or 99, 1.5)

        player.set_volume(55)
        self.assertAlmostEqual(player.get_volume(), 55, delta=0.1)

        player.set_paused(False)
        deadline = time.time() + 8
        while time.time() < deadline and not ended.is_set():
            time.sleep(0.1)
        self.assertTrue(ended.is_set(), "EOF event never fired")
        player.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
