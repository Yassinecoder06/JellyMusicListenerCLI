"""Shared data model for tracks, albums, artists and playlists."""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_JELLYFIN = "jellyfin"
SOURCE_LOCAL = "local"


def fmt_duration(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return "-:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: str = ""
    album_id: str = ""
    year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    genre: str | None = None
    album_artist: str | None = None
    duration: float | None = None
    source: str = SOURCE_JELLYFIN
    stream_ref: str = ""
    cover_key: str = ""
    playlist_entry_id: str | None = None
    mb_artist_id: str | None = None
    mb_album_id: str | None = None
    mb_recording_id: str | None = None
    needs_review: bool = False

    @property
    def duration_text(self) -> str:
        return fmt_duration(self.duration)

    def line_label(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass
class Album:
    id: str
    name: str
    artist: str
    year: int | None = None
    track_count: int = 0
    source: str = SOURCE_JELLYFIN
    cover_key: str = ""
    genre: str | None = None
    mb_album_id: str | None = None
    mb_release_id: str | None = None


@dataclass
class Artist:
    id: str
    name: str
    album_count: int = 0
    source: str = SOURCE_JELLYFIN
    normalized_name: str = ""
    mb_artist_id: str | None = None


@dataclass
class Playlist:
    id: str
    name: str
    track_count: int = 0
    source: str = SOURCE_JELLYFIN


@dataclass
class LibrarySnapshot:
    artists: list[Artist] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
