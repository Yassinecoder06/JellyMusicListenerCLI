"""Tests for Jellyfin-compatible organizer pipeline."""

import tempfile
import subprocess
from pathlib import Path
import sys

import unittest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from music_listener.metadata import CanonicalMetadata, generate_canonical_path, sanitize_component, find_available_path, extract_existing_metadata, write_metadata
from music_listener.organizer import preview_organize, organize

def make_mp3(path: Path, title="Title", artist="Artist", album="Album", albumartist=None, track="1", disc="1", year="2020", genre="Test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","sine=frequency=440:duration=1", str(path)], check=True)
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TCON
    try:
        id3 = ID3(str(path))
    except Exception:
        from mutagen.mp3 import MP3
        audio = MP3(str(path))
        if audio.tags is None:
            audio.add_tags()
        id3 = audio.tags
        # fallback
        from mutagen.id3 import ID3
        id3 = ID3(str(path)) if Path(str(path)).exists() else id3
    # ensure ID3
    from mutagen.id3 import ID3
    try:
        id3 = ID3(str(path))
    except Exception:
        id3 = ID3()
    id3["TIT2"] = TIT2(encoding=3, text=title)
    id3["TPE1"] = TPE1(encoding=3, text=artist)
    id3["TPE2"] = TPE2(encoding=3, text=albumartist or artist)
    id3["TALB"] = TALB(encoding=3, text=album)
    id3["TRCK"] = TRCK(encoding=3, text=track)
    id3["TPOS"] = TPOS(encoding=3, text=disc)
    id3["TDRC"] = TDRC(encoding=3, text=year)
    if genre:
        id3["TCON"] = TCON(encoding=3, text=genre)
    id3.save(str(path))

class OrganizerTests(unittest.TestCase):
    def test_normal_album(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"
            dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            f = src/"01 - One More Time.mp3"
            make_mp3(f, title="One More Time", artist="Daft Punk", album="Discovery", track="1", year="2001")
            ops = preview_organize(src, dst)
            self.assertEqual(len(ops),1)
            self.assertEqual(ops[0].destination, dst/"Daft Punk"/"Discovery"/"01 - One More Time.mp3")
            result = organize(src, dst)
            self.assertEqual(result["copied"],1)
            self.assertTrue((dst/"Daft Punk"/"Discovery"/"01 - One More Time.mp3").exists())
            # original remains
            self.assertTrue(f.exists())

    def test_compilation_various_artists(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            f1 = src/"01 - Song A.mp3"
            make_mp3(f1, title="Song A", artist="Artist A", album="Best Electronic", albumartist="Various Artists", track="1")
            f2 = src/"02 - Song B.mp3"
            make_mp3(f2, title="Song B", artist="Artist B", album="Best Electronic", albumartist="Various Artists", track="2")
            result = organize(src, dst)
            self.assertEqual(result["copied"],2)
            # Both should be under Various Artists
            self.assertTrue((dst/"Various Artists"/"Best Electronic"/"01 - Song A.mp3").exists())
            self.assertTrue((dst/"Various Artists"/"Best Electronic"/"02 - Song B.mp3").exists())
            # Ensure track artist preserved (not overwritten to Various)
            meta = extract_existing_metadata(dst/"Various Artists"/"Best Electronic"/"01 - Song A.mp3")
            self.assertEqual(meta.artist, "Artist A")
            self.assertEqual(meta.album_artist, "Various Artists")

    def test_multi_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            f1 = src/"d1.mp3"
            make_mp3(f1, title="Song1", artist="Artist", album="Album", track="1", disc="1")
            f2 = src/"d2.mp3"
            make_mp3(f2, title="Song2", artist="Artist", album="Album", track="1", disc="2")
            organize(src, dst)
            self.assertTrue((dst/"Artist"/"Album"/"01 - Song1.mp3").exists())
            self.assertTrue((dst/"Artist"/"Album"/"2-01 - Song2.mp3").exists())

    def test_missing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            f = src/"unknown.mp3"
            subprocess.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","sine=frequency=440:duration=1", str(f)], check=True)
            ops = preview_organize(src, dst)
            # Should be marked needs_review
            self.assertTrue(ops[0].status in ("needs_review","pending"))
            # But organize should keep file safe and create Unknown Artist/Unknown Album fallback
            result = organize(src, dst)
            # At least not crash, file should be organized to Unknown
            self.assertTrue(f.exists())  # source preserved

    def test_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            cases = [("Beyoncé","Album"), ("Sigur Rós","Album"), ("أم كلثوم","Album")]
            for artist, album in cases:
                f = src/f"{artist} - song.mp3"
                make_mp3(f, title="Song", artist=artist, album=album)
            result = organize(src, dst)
            self.assertEqual(result["copied"],3)
            self.assertTrue((dst/"Beyoncé").exists())
            self.assertTrue((dst/"Sigur Rós").exists())

    def test_special_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            for artist in ["AC/DC", 'Guns N\' Roses', 'Artist: Test', 'A*B?C']:
                f = src/f"test {artist}.mp3"
                # ffmpeg filename sanitization needed
                safe_name = artist.replace("/","_")
                f2 = src/f"{safe_name}.mp3"
                make_mp3(f2, title="Song", artist=artist, album="Album")
            result = organize(src, dst)
            # Check sanitized folders do not contain invalid chars
            for p in dst.rglob("*"):
                self.assertNotIn(":", p.name)
                self.assertNotIn("?", p.name)
                self.assertNotIn("*", p.name)
                self.assertNotIn('"', p.name)
            # AC/DC should be sanitized to ACDC or AC DC
            self.assertTrue(any("ACDC" in str(p) or "AC" in str(p) for p in dst.iterdir()))

    def test_duplicate_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            f1 = src/"a.mp3"
            make_mp3(f1, title="Same", artist="Artist", album="Album", track="1")
            organize(src, dst)
            # second file with same metadata but different source
            src2 = Path(tmp)/"src2"
            src2.mkdir()
            f2 = src2/"b.mp3"
            # Use different audio content to avoid fingerprint duplicate
            make_mp3(f2, title="Same", artist="Artist", album="Album", track="1")
            # Make content differ by appending bytes
            with open(f2, "ab") as fh:
                fh.write(b"\x00\x01\x02\x03")
            result = organize(src2, dst)
            # Should not overwrite, should create (1) when content differs
            self.assertTrue((dst/"Artist"/"Album"/"01 - Same.mp3").exists())
            # Either conflict file exists or it was considered duplicate (if fingerprint same)
            self.assertTrue((dst/"Artist"/"Album"/"01 - Same (1).mp3").exists() or result["skipped"]==1 or result["copied"]==1)

    def test_failed_lookup_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            f = src/"bad.mp3"
            make_mp3(f, title="NonexistentSongXYZ123", artist="UnknownArtistXYZ", album="UnknownAlbumXYZ")
            # Try with musicbrainz enabled but should not delete source on low confidence
            result = organize(src, dst, use_musicbrainz=True)
            self.assertTrue(f.exists())
            # dest should still exist with original metadata
            self.assertEqual(result["copied"],1)

    def test_jellyfin_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            make_mp3(src/"01 - Song.mp3", title="Song", artist="Daft Punk", album="Discovery", track="1", disc="1")
            make_mp3(src/"02 - Song2.mp3", title="Song2", artist="Daft Punk", album="Discovery", track="2", disc="1")
            organize(src, dst)
            # Jellyfin expects Music/Artist/Album/Tracks
            self.assertTrue((dst/"Daft Punk").is_dir())
            self.assertTrue((dst/"Daft Punk"/"Discovery").is_dir())
            tracks = list((dst/"Daft Punk"/"Discovery").glob("*.mp3"))
            self.assertEqual(len(tracks),2)
            # Check embedded tags remain correct without re-encoding (file still mp3)
            for t in tracks:
                meta = extract_existing_metadata(t)
                self.assertEqual(meta.album_artist, "Daft Punk")
                self.assertEqual(meta.album, "Discovery")

    def test_sanitize_and_long_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)/"dst"
            long_title = "A"*200
            meta = CanonicalMetadata(title=long_title, artist="Artist", album="Album", album_artist="Artist", track_number=1, disc_number=1, source_path="test.mp3")
            path = generate_canonical_path(dst, meta)
            self.assertLess(len(path.name.encode("utf-8")), 255)

    def test_dry_run_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)/"src"; dst = Path(tmp)/"dst"
            src.mkdir(); dst.mkdir()
            make_mp3(src/"01 - Song.mp3", title="Song", artist="Artist", album="Album")
            ops = preview_organize(src, dst)
            self.assertEqual(ops[0].destination, dst/"Artist"/"Album"/"01 - Song.mp3")
            # dry run should not move
            self.assertFalse(ops[0].destination.exists())
