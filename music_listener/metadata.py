"""Canonical Jellyfin-compatible metadata engine.

Implements:
- sanitized filesystem names (Jellyfin safe)
- extraction/normalization of embedded tags
- optional MusicBrainz enrichment
- Jellyfin hierarchy: Music Root → Album Artist → Album → Tracks
- multi-disc, compilation, unicode, duplicate handling
- tag writing without re-encoding
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("jml.metadata")

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRAILING_DOTS_RE = re.compile(r'[. ]+$')
YEAR_RE = re.compile(r'(\d{4})')
SAFE_NAME_MAX = 120  # per component, leaves room for 255 full path


@dataclass
class CanonicalMetadata:
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    year: str = ""  # 4-digit
    genre: str = ""
    mb_artist_id: str = ""
    mb_album_id: str = ""
    mb_release_id: str = ""
    mb_recording_id: str = ""
    needs_review: bool = False
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "album_artist": self.album_artist,
            "album": self.album,
            "track_number": self.track_number,
            "disc_number": self.disc_number,
            "year": self.year,
            "genre": self.genre,
            "mb_artist_id": self.mb_artist_id,
            "mb_album_id": self.mb_album_id,
            "mb_release_id": self.mb_release_id,
            "mb_recording_id": self.mb_recording_id,
        }


def sanitize_component(name: str, fallback: str = "Unknown") -> str:
    """Make a filesystem-safe path component for Jellyfin."""
    name = unicodedata.normalize("NFC", name or "").strip()
    if not name:
        return fallback
    # Replace invalid chars with empty, keep unicode/accents
    name = INVALID_CHARS_RE.sub("", name)
    # Don't allow pure dots
    name = TRAILING_DOTS_RE.sub("", name).strip()
    if not name:
        return fallback
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    # Truncate safely
    if len(name) > SAFE_NAME_MAX:
        name = name[:SAFE_NAME_MAX].rstrip()
    # Avoid Windows reserved names
    reserved = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
    if name.upper() in reserved:
        name = f"{name}_"
    return name


def normalize_for_comparison(name: str) -> str:
    """Normalized key for deduplication, preserves display."""
    n = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def _parse_year(raw: str) -> str:
    m = YEAR_RE.search(raw or "")
    return m.group(1) if m else ""


def extract_existing_metadata(path: Path) -> CanonicalMetadata:
    """Read embedded tags without overwriting."""
    meta = CanonicalMetadata(source_path=str(path))
    # fallback from filename
    stem = path.stem
    # try 01 - Title or 1-01 - Title
    disc = None
    track = None
    title_guess = stem
    m = re.match(r"^(?:(\d+)[-_])?(\d{1,3})\s*[-._]\s*(.+)$", stem)
    if m:
        if m.group(1) and m.group(3):
            # disc-track form 1-01
            try:
                disc = int(m.group(1))
                track = int(m.group(2))
                title_guess = m.group(3)
            except ValueError:
                pass
        elif m.group(2):
            try:
                track = int(m.group(2))
                title_guess = m.group(3)
            except ValueError:
                pass
    # fallback artist/album from parent folders
    try:
        album_guess = path.parent.name
        artist_guess = path.parent.parent.name
    except Exception:
        album_guess = ""
        artist_guess = ""

    meta.title = title_guess.strip()
    meta.album = album_guess.strip()
    meta.artist = artist_guess.strip()
    if track:
        meta.track_number = track
    if disc:
        meta.disc_number = disc

    # Now try mutagen - always try easy first, then raw for MBIDs
    try:
        from mutagen import File as MutagenFile  # type: ignore

        # Easy tags - works for most formats via mutagen easy interface
        try:
            audio_easy = MutagenFile(str(path), easy=True)
            if audio_easy is not None and audio_easy.tags:
                tags = audio_easy.tags or {}
                meta.title = (tags.get("title") or [meta.title])[0] if tags.get("title") else meta.title
                meta.artist = (tags.get("artist") or [meta.artist])[0] if tags.get("artist") else meta.artist
                meta.album = (tags.get("album") or [meta.album])[0] if tags.get("album") else meta.album
                aa = tags.get("albumartist") or tags.get("album artist") or tags.get("album_artist") or []
                if aa:
                    meta.album_artist = aa[0]
                meta.genre = (tags.get("genre") or [""])[0] if tags.get("genre") else meta.genre
                tn = (tags.get("tracknumber") or [""])[0]
                if tn:
                    try:
                        meta.track_number = int(str(tn).split("/")[0])
                    except ValueError:
                        pass
                dn = (tags.get("discnumber") or [""])[0]
                if dn:
                    try:
                        meta.disc_number = int(str(dn).split("/")[0])
                    except ValueError:
                        pass
                yr = (tags.get("date") or tags.get("year") or [""])[0]
                if yr:
                    meta.year = _parse_year(str(yr))
        except Exception:
            pass
        # Raw tags for MBIDs and additional fields
        try:
            audio_raw = MutagenFile(str(path), easy=False)
            if audio_raw is not None and hasattr(audio_raw, "tags") and audio_raw.tags:
                raw_tags = audio_raw.tags
                # Vorbis/FLAC style
                for k in ["MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_RELEASETRACKID"]:
                    if k in raw_tags:
                        val = raw_tags[k]
                        v = val[0] if isinstance(val, list) else str(val)
                        if k == "MUSICBRAINZ_ARTISTID":
                            meta.mb_artist_id = str(v)
                        elif k == "MUSICBRAINZ_ALBUMID":
                            meta.mb_album_id = str(v)
                        elif k == "MUSICBRAINZ_RELEASEGROUPID":
                            meta.mb_release_id = str(v)
                        elif k in ("MUSICBRAINZ_TRACKID", "MUSICBRAINZ_RELEASETRACKID"):
                            meta.mb_recording_id = str(v)
                # ID3 TXXX
                for key, val in list(raw_tags.items()):
                    if isinstance(key, str) and key.startswith("TXXX:"):
                        desc = key[5:].lower()
                        v = val.text[0] if hasattr(val, "text") else str(val)
                        if "musicbrainz artist" in desc:
                            meta.mb_artist_id = str(v)
                        elif "musicbrainz album id" in desc:
                            meta.mb_album_id = str(v)
                        elif "musicbrainz release group" in desc or "musicbrainz release id" == desc:
                            meta.mb_release_id = str(v)
                        elif "musicbrainz recording" in desc or "musicbrainz track id" in desc:
                            meta.mb_recording_id = str(v)
                # Also try to get albumartist if not yet
                if not meta.album_artist:
                    for key in ["TPE2", "TXXX:AlbumArtist", "TXXX:ALBUMARTIST"]:
                        if key in raw_tags:
                            v = raw_tags[key]
                            # TPE2 is text frame
                            try:
                                txt = v.text[0] if hasattr(v, "text") else str(v)
                                if txt:
                                    meta.album_artist = str(txt)
                                    break
                            except Exception:
                                pass
        except Exception:
            pass
        # Final albumartist fallback
        if not meta.album_artist:
            try:
                audio_easy2 = MutagenFile(str(path), easy=True)
                if audio_easy2 is not None and audio_easy2.tags:
                    for key in ["albumartist", "album artist", "ensemble", "album_artist"]:
                        if key in audio_easy2.tags:
                            meta.album_artist = audio_easy2.tags[key][0]
                            break
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[METADATA] could not read tags from {path}: {e}")

    # Post-normalization
    if not meta.album_artist:
        # If compilation heuristic: if artist != album and Various Artists known, keep as is
        # Default album_artist = artist unless compilation
        meta.album_artist = meta.artist
    # Ensure title/artist/album not empty
    if not meta.title:
        meta.title = Path(path).stem
        meta.needs_review = True
    if not meta.artist:
        meta.artist = "Unknown Artist"
        meta.needs_review = True
    if not meta.album:
        meta.album = "Unknown Album"
        meta.needs_review = True
    if not meta.album_artist:
        meta.album_artist = meta.artist
    if meta.track_number is None:
        meta.track_number = 1
        # don't mark review just for track number, but maybe
    if meta.disc_number is None:
        meta.disc_number = 1
    # Clean unicode NFC
    for f in ["title", "artist", "album", "album_artist", "genre"]:
        setattr(meta, f, unicodedata.normalize("NFC", getattr(meta, f).strip()))
    return meta


def enrich_with_musicbrainz(meta: CanonicalMetadata, enable: bool = True) -> CanonicalMetadata:
    """Attempt MusicBrainz identification. Never blindly overwrite good data."""
    if not enable:
        return meta
    # If we already have MBIDs, assume good
    if meta.mb_recording_id and meta.mb_artist_id and meta.mb_album_id:
        return meta
    # Only attempt if we have at least artist+title
    if not meta.artist or meta.artist == "Unknown Artist" or not meta.title:
        return meta
    try:
        import musicbrainzngs  # type: ignore

        musicbrainzngs.set_useragent("JellyfinMusicListenerCLI", "1.1.2", "https://github.com/Yassinecoder06/JellyfinMusicListenerCLI")
        # Search recording
        result = musicbrainzngs.search_recordings(artist=meta.artist, recording=meta.title, limit=3)
        recordings = result.get("recording-list", [])
        if not recordings:
            logger.info(f"[IDENTIFICATION] No MusicBrainz match for {meta.artist} - {meta.title}")
            meta.needs_review = True
            return meta
        # Simple confidence: exact title + artist match
        best = None
        best_score = -1
        for rec in recordings:
            score = int(rec.get("ext:score", "0") or 0)
            title = rec.get("title", "").lower()
            artist_credit = rec.get("artist-credit", [])
            artist_name = artist_credit[0].get("artist", {}).get("name", "").lower() if artist_credit else ""
            # boost if close
            if title == meta.title.lower():
                score += 20
            if artist_name == meta.artist.lower():
                score += 20
            if score > best_score:
                best_score = score
                best = rec
        if best is None or best_score < 70:
            logger.info(f"[IDENTIFICATION] Low confidence MusicBrainz match ({best_score}) for {meta.artist} - {meta.title}")
            meta.needs_review = True
            return meta
        # Extract fields, but do not overwrite if existing is meaningful and different? We update only if confidence high and missing
        try:
            mb_title = best.get("title")
            if mb_title and (meta.needs_review or not meta.title or meta.title.lower() in mb_title.lower() or mb_title.lower() in meta.title.lower()):
                # keep original title if user has good title? We preserve original if not needs_review? Spec: do not blindly overwrite. So only fill missing or if review flag
                pass  # keep original title
            # artist credit
            ac = best.get("artist-credit", [])
            if ac:
                mb_artist = ac[0].get("artist", {}).get("name")
                mb_artist_id = ac[0].get("artist", {}).get("id")
                if mb_artist_id:
                    meta.mb_artist_id = mb_artist_id
                # album
                releases = best.get("release-list", [])
                if releases:
                    rel = releases[0]
                    mb_album = rel.get("title")
                    mb_album_id = rel.get("id")
                    mb_release_group = rel.get("release-group", {}).get("id", "")
                    if mb_album and (meta.album in ("Unknown Album", "Single", "Unknown", "") or meta.needs_review):
                        meta.album = mb_album
                    if mb_album_id:
                        meta.mb_album_id = mb_album_id
                    if mb_release_group:
                        meta.mb_release_id = mb_release_group
                    # album artist from release?
                    # Use release artist if available
                    # For now keep meta.album_artist as is unless it's Unknown
            meta.mb_recording_id = best.get("id", meta.mb_recording_id)
            logger.info(f"[IDENTIFICATION] MusicBrainz match found: {meta.artist} - {meta.title} -> {meta.mb_recording_id} (score {best_score})")
        except Exception as e:
            logger.warning(f"[IDENTIFICATION] error parsing MB result: {e}")
    except ImportError:
        logger.debug("[IDENTIFICATION] musicbrainzngs not installed, skipping")
    except Exception as e:
        logger.warning(f"[IDENTIFICATION] MusicBrainz lookup failed: {e}")
        meta.needs_review = True
    return meta


def generate_canonical_path(root: Path, meta: CanonicalMetadata) -> Path:
    """Generate canonical filesystem path: root / AlbumArtist / Album / track file."""
    # Artist handling: Album Artist is filesystem parent, not track artist for compilations
    # For Various Artists compilations, folder is "Various Artists" but track artist preserved in tags
    album_artist = sanitize_component(meta.album_artist or meta.artist, fallback="Unknown Artist")
    album = sanitize_component(meta.album, fallback="Unknown Album")
    title = sanitize_component(meta.title, fallback="Unknown Title")
    ext = Path(meta.source_path).suffix.lower() if meta.source_path else ".mp3"
    if not ext:
        ext = ".mp3"
    # Multi-disc handling: Jellyfin compatible - keep in same Album folder, prefix disc number
    track_num = f"{meta.track_number:02d}" if meta.track_number else "01"
    disc = meta.disc_number or 1
    if disc > 1:
        filename = f"{disc}-{track_num} - {title}{ext}"
    else:
        filename = f"{track_num} - {title}{ext}"
    # Handle extremely long filename: truncate title part to fit 255
    # Full path limit is OS dependent, but filename alone should be <255
    if len(filename.encode("utf-8")) > 240:
        # truncate title
        allowed = 240 - len(f"{track_num} - {ext}") - (len(f"{disc}-") if disc > 1 else 0)
        # approximate char truncation
        title_trunc = title.encode("utf-8")[:allowed].decode("utf-8", errors="ignore")
        title_trunc = title_trunc.rstrip()
        if disc > 1:
            filename = f"{disc}-{track_num} - {title_trunc}{ext}"
        else:
            filename = f"{track_num} - {title_trunc}{ext}"
    return root / album_artist / album / filename


def find_available_path(dest: Path) -> Path:
    """Deterministic conflict strategy: 01 - Song.flac, 01 - Song (1).flac etc."""
    if not dest.exists():
        return dest
    # If same file already exists with same size/hash, caller handles duplicate detection before this
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
        if counter > 100:
            # fallback to hash
            h = hashlib.md5(str(dest).encode()).hexdigest()[:6]
            return parent / f"{stem} ({h}){suffix}"


def file_fingerprint(path: Path) -> str:
    """Simple fingerprint: size + first 64k hash. For duplicate detection."""
    try:
        size = path.stat().st_size
        h = hashlib.md5()
        with open(path, "rb") as f:
            chunk = f.read(65536)
            h.update(chunk)
        return f"{size}-{h.hexdigest()[:8]}"
    except Exception:
        return ""


def write_metadata(path: Path, meta: CanonicalMetadata) -> bool:
    """Write canonical metadata to file without re-encoding. Returns success."""
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TCON, TXXX
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4

        ext = path.suffix.lower()
        if ext == ".mp3":
            try:
                audio = MutagenFile(str(path))
                if audio is None:
                    # Try ID3 directly
                    audio = ID3(str(path))
                # Ensure ID3
                if isinstance(audio, ID3):
                    id3 = audio
                else:
                    # Convert to ID3 if needed
                    if hasattr(audio, "tags") and audio.tags is not None:
                        # try to get ID3
                        try:
                            id3 = ID3(str(path))
                        except Exception:
                            id3 = ID3()
                    else:
                        id3 = ID3()
                # Set frames
                id3["TIT2"] = TIT2(encoding=3, text=meta.title)
                id3["TPE1"] = TPE1(encoding=3, text=meta.artist)
                id3["TPE2"] = TPE2(encoding=3, text=meta.album_artist)
                id3["TALB"] = TALB(encoding=3, text=meta.album)
                id3["TRCK"] = TRCK(encoding=3, text=str(meta.track_number or 1))
                id3["TPOS"] = TPOS(encoding=3, text=str(meta.disc_number or 1))
                if meta.year:
                    id3["TDRC"] = TDRC(encoding=3, text=meta.year)
                if meta.genre:
                    id3["TCON"] = TCON(encoding=3, text=meta.genre)
                # MBIDs via TXXX
                if meta.mb_artist_id:
                    id3["TXXX:MusicBrainz Artist Id"] = TXXX(encoding=3, desc="MusicBrainz Artist Id", text=meta.mb_artist_id)
                if meta.mb_album_id:
                    id3["TXXX:MusicBrainz Album Id"] = TXXX(encoding=3, desc="MusicBrainz Album Id", text=meta.mb_album_id)
                if meta.mb_release_id:
                    id3["TXXX:MusicBrainz Release Group Id"] = TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=meta.mb_release_id)
                if meta.mb_recording_id:
                    id3["TXXX:MusicBrainz Track Id"] = TXXX(encoding=3, desc="MusicBrainz Track Id", text=meta.mb_recording_id)
                id3.save(str(path))
                logger.info(f"[WRITE TAGS] success {path} -> {meta.artist} / {meta.album} / {meta.title}")
                return True
            except Exception as e:
                logger.warning(f"[WRITE TAGS] mp3 failed {path}: {e}")
                return False
        elif ext == ".flac":
            try:
                audio = FLAC(str(path))
                audio["TITLE"] = meta.title
                audio["ARTIST"] = meta.artist
                audio["ALBUMARTIST"] = meta.album_artist
                audio["ALBUM"] = meta.album
                audio["TRACKNUMBER"] = str(meta.track_number or 1)
                audio["DISCNUMBER"] = str(meta.disc_number or 1)
                if meta.year:
                    audio["DATE"] = meta.year
                if meta.genre:
                    audio["GENRE"] = meta.genre
                if meta.mb_artist_id:
                    audio["MUSICBRAINZ_ARTISTID"] = meta.mb_artist_id
                if meta.mb_album_id:
                    audio["MUSICBRAINZ_ALBUMID"] = meta.mb_album_id
                if meta.mb_release_id:
                    audio["MUSICBRAINZ_RELEASEGROUPID"] = meta.mb_release_id
                if meta.mb_recording_id:
                    audio["MUSICBRAINZ_TRACKID"] = meta.mb_recording_id
                audio.save()
                logger.info(f"[WRITE TAGS] success {path}")
                return True
            except Exception as e:
                logger.warning(f"[WRITE TAGS] flac failed {path}: {e}")
                return False
        elif ext in (".m4a", ".mp4", ".aac"):
            try:
                audio = MP4(str(path))
                audio["\xa9nam"] = [meta.title]
                audio["\xa9ART"] = [meta.artist]
                audio["aART"] = [meta.album_artist]
                audio["\xa9alb"] = [meta.album]
                audio["trkn"] = [(meta.track_number or 1, 0)]
                audio["disk"] = [(meta.disc_number or 1, 0)]
                if meta.year:
                    audio["\xa9day"] = [meta.year]
                if meta.genre:
                    audio["\xa9gen"] = [meta.genre]
                # MBIDs as freeform
                if meta.mb_artist_id:
                    audio["----:com.apple.iTunes:MusicBrainz Artist Id"] = [meta.mb_artist_id.encode()]
                if meta.mb_album_id:
                    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [meta.mb_album_id.encode()]
                if meta.mb_recording_id:
                    audio["----:com.apple.iTunes:MusicBrainz Track Id"] = [meta.mb_recording_id.encode()]
                audio.save()
                logger.info(f"[WRITE TAGS] success {path}")
                return True
            except Exception as e:
                logger.warning(f"[WRITE TAGS] m4a failed {path}: {e}")
                return False
        elif ext in (".ogg", ".opus"):
            try:
                from mutagen.oggvorbis import OggVorbis
                from mutagen.oggopus import OggOpus

                cls = OggVorbis if ext == ".ogg" else OggOpus
                audio = cls(str(path))
                audio["TITLE"] = meta.title
                audio["ARTIST"] = meta.artist
                audio["ALBUMARTIST"] = meta.album_artist
                audio["ALBUM"] = meta.album
                audio["TRACKNUMBER"] = str(meta.track_number or 1)
                audio["DISCNUMBER"] = str(meta.disc_number or 1)
                if meta.year:
                    audio["DATE"] = meta.year
                if meta.genre:
                    audio["GENRE"] = meta.genre
                audio.save()
                logger.info(f"[WRITE TAGS] success {path}")
                return True
            except Exception as e:
                logger.warning(f"[WRITE TAGS] ogg/opus failed {path}: {e}")
                return False
        else:
            # For wav/wma, try easy
            try:
                audio = MutagenFile(str(path), easy=True)
                if audio is not None:
                    audio["title"] = [meta.title]
                    audio["artist"] = [meta.artist]
                    audio["album"] = [meta.album]
                    audio["albumartist"] = [meta.album_artist]
                    audio["tracknumber"] = [str(meta.track_number or 1)]
                    audio.save()
                    return True
            except Exception as e:
                logger.warning(f"[WRITE TAGS] generic failed {path}: {e}")
            logger.info(f"[WRITE TAGS] skipped unsupported format {path.suffix} for {path}")
            return True  # not fatal, file can still be moved
    except Exception as e:
        logger.error(f"[WRITE TAGS] error {path}: {e}")
        return False
