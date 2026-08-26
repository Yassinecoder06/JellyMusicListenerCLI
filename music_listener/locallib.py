"""Local library scanner for Jellyfin-style folder layouts.

Expected layout (matching the Jellyfin Music Downloader organizer):

    ROOT/Artist/Album (Year)/01 - Title.mp3
    ROOT/Artist/Album (Year)/cover.jpg

Loose audio files directly under ROOT are grouped into "[No Artist]".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .models import SOURCE_LOCAL, Album, Artist, LibrarySnapshot, Track

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}

COVER_NAMES = (
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
    "front.jpg",
    "front.png",
    "album.jpg",
    "album.png",
    "art.jpg",
    "art.png",
)

ALBUM_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
TRACK_NUMBER_RE = re.compile(r"^(\d{1,3})\s*[-._)\s]+\s*(.+)$")

NO_ARTIST = "[No Artist]"
NO_ALBUM = "[No Album]"


@dataclass
class LocalLibrary:
    root: str
    snapshot: LibrarySnapshot = field(default_factory=LibrarySnapshot)

    def scan(self) -> LibrarySnapshot:
        root = Path(self.root).expanduser()
        if not root.is_dir():
            raise NotADirectoryError(f"Not a folder: {root}")

        artists: dict[str, Artist] = {}
        albums: dict[str, Album] = {}
        tracks: list[Track] = []

        loose_files: list[Path] = []
        artist_dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: _sort_key(p.name),
        )

        for artist_dir in artist_dirs:
            album_dirs = sorted(
                (p for p in artist_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: _sort_key(p.name),
            )
            loose_in_artist = [
                p for p in artist_dir.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
            ]
            if not album_dirs and not loose_in_artist:
                continue

            artist_name = _clean(artist_dir.name)
            artist_id = artist_dir.name
            artists.setdefault(
                artist_id,
                Artist(id=artist_id, name=artist_name, source=SOURCE_LOCAL),
            )

            for album_dir in album_dirs:
                files = sorted(
                    (p for p in album_dir.iterdir() if p.is_file()),
                    key=lambda p: _sort_key(p.name),
                )
                audio_files = [p for p in files if p.suffix.lower() in AUDIO_EXTENSIONS]
                if not audio_files:
                    continue
                album_name, year = _parse_album_dir(album_dir.name)
                album_id = f"{artist_dir.name}/{album_dir.name}"
                album = Album(
                    id=album_id,
                    name=album_name,
                    artist=artist_name,
                    year=year,
                    track_count=0,
                    source=SOURCE_LOCAL,
                    cover_key=album_id,
                )
                albums[album_id] = album

                for path in audio_files:
                    track = _track_from_file(path, artist_name, album_name, year, album_id)
                    tracks.append(track)
                    album.track_count += 1

            for path in loose_in_artist:
                track = _track_from_file(path, artist_name, "[Singles]", None, "")
                tracks.append(track)
                singles_id = f"{artist_dir.name}/[Singles]"
                album = albums.get(singles_id)
                if album is None:
                    album = Album(
                        id=singles_id,
                        name="[Singles]",
                        artist=artist_name,
                        source=SOURCE_LOCAL,
                        cover_key="",
                        track_count=0,
                    )
                    albums[singles_id] = album
                album.track_count += 1
        for path in sorted(root.glob("*"), key=lambda p: _sort_key(p.name)):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                loose_files.append(path)
        if loose_files:
            artist_id = "__no_artist__"
            artists.setdefault(
                artist_id, Artist(id=artist_id, name=NO_ARTIST, source=SOURCE_LOCAL)
            )
            album_id = "__no_artist__/__no_album__"
            album = Album(
                id=album_id,
                name=NO_ALBUM,
                artist=NO_ARTIST,
                source=SOURCE_LOCAL,
                cover_key="",
            )
            albums[album_id] = album
            for path in loose_files:
                track = _track_from_file(path, NO_ARTIST, NO_ALBUM, None, album_id)
                tracks.append(track)
                album.track_count += 1

        artist_album_counts: dict[str, int] = {}
        for album in albums.values():
            artist_album_counts[album.artist] = artist_album_counts.get(album.artist, 0) + 1
        for artist in artists.values():
            artist.album_count = artist_album_counts.get(artist.name, 0)

        self.snapshot = LibrarySnapshot(
            artists=sorted(artists.values(), key=lambda a: _sort_key(a.name)),
            albums=sorted(
                albums.values(),
                key=lambda a: (_sort_key(a.artist), a.year or 9999, _sort_key(a.name)),
            ),
            tracks=sorted(
                tracks,
                key=lambda t: (_sort_key(t.artist), _sort_key(t.album), t.track_number or 9999, _sort_key(t.title)),
            ),
        )
        return self.snapshot


def find_cover(album_dir: Path) -> Path | None:
    for name in COVER_NAMES:
        candidate = album_dir / name
        if candidate.is_file():
            return candidate
    try:
        for entry in sorted(album_dir.iterdir()):
            if entry.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                return entry
    except OSError:
        pass
    return None


def _track_from_file(
    path: Path,
    artist_fallback: str,
    album_fallback: str,
    year: int | None,
    album_id: str,
) -> Track:
    number: int | None = None
    title = path.stem
    match = TRACK_NUMBER_RE.match(title)
    if match:
        number = int(match.group(1))
        title = match.group(2)

    duration = None
    tag_title = ""
    tag_artist = ""
    tag_album = ""
    tag_number: int | None = None
    tag_year: int | None = year
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path), easy=True)
        if audio is not None:
            if audio.info is not None:
                duration = float(getattr(audio.info, "length", 0) or 0) or None
            tags = audio.tags or {}
            tag_title = (tags.get("title") or [""])[0]
            tag_artist = (tags.get("artist") or [""])[0]
            tag_album = (tags.get("album") or [""])[0]
            raw_number = (tags.get("tracknumber") or [""])[0]
            num_match = re.match(r"^(\d+)", raw_number)
            if num_match:
                tag_number = int(num_match.group(1))
            raw_year = (tags.get("date") or [""])[0]
            year_match = re.match(r"^(\d{4})", raw_year)
            if year_match:
                tag_year = int(year_match.group(1))
    except Exception:
        pass

    return Track(
        id=str(path.resolve()),
        title=_clean(tag_title) or _clean(title) or path.name,
        artist=_clean(tag_artist) or artist_fallback,
        album=_clean(tag_album) or album_fallback,
        album_id=album_id,
        year=tag_year,
        track_number=tag_number if tag_number is not None else number,
        duration=duration,
        source=SOURCE_LOCAL,
        stream_ref=str(path.resolve()),
        cover_key=f"local:{album_id}" if album_id else "",
    )


def _parse_album_dir(name: str) -> tuple[str, int | None]:
    match = ALBUM_YEAR_RE.match(name)
    if match:
        return _clean(match.group(1)) or name, int(match.group(2))
    return _clean(name) or name, None


def _clean(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _sort_key(value: str) -> str:
    value = value.lower().lstrip("'\"([ ")
    if value.startswith("the "):
        value = value[4:]
    return unicodedata.normalize("NFKD", value)
