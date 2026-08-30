"""Persistent playlists for tracks in a local library."""

from __future__ import annotations

import json
import stat
import uuid
from dataclasses import replace
from pathlib import Path

from . import config as cfg
from .models import SOURCE_LOCAL, Playlist, Track


def _path() -> Path:
    return cfg.CONFIG_PATH.with_name("local-playlists.json")


def _load() -> list[dict[str, object]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    playlists = data.get("playlists", []) if isinstance(data, dict) else []
    return [item for item in playlists if isinstance(item, dict)]


def _save(playlists: list[dict[str, object]]) -> None:
    path = _path()
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cfg.CONFIG_DIR.chmod(stat.S_IRWXU)
    except OSError:
        pass
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"playlists": playlists}, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    temporary.replace(path)


def playlists(library_tracks: list[Track]) -> list[Playlist]:
    available = {track.id for track in library_tracks}
    return [
        Playlist(
            id=str(item.get("id", "")),
            name=str(item.get("name", "Untitled")),
            track_count=sum(track_id in available for track_id in item.get("track_ids", [])),
            source=SOURCE_LOCAL,
        )
        for item in _load()
        if item.get("id") and item.get("name")
    ]


def tracks(playlist_id: str, library_tracks: list[Track]) -> list[Track]:
    by_id = {track.id: track for track in library_tracks}
    for item in _load():
        if item.get("id") != playlist_id:
            continue
        return [
            replace(by_id[track_id], playlist_entry_id=track_id)
            for track_id in item.get("track_ids", [])
            if track_id in by_id
        ]
    return []


def create(name: str, track_ids: list[str] | None = None) -> Playlist:
    name = name.strip()
    if not name:
        raise ValueError("Playlist name cannot be empty")
    playlist = {"id": uuid.uuid4().hex, "name": name, "track_ids": list(track_ids or [])}
    saved = _load()
    saved.append(playlist)
    _save(saved)
    return Playlist(
        id=playlist["id"], name=name, track_count=len(playlist["track_ids"]), source=SOURCE_LOCAL
    )


def add_tracks(playlist_id: str, track_ids: list[str]) -> None:
    saved = _load()
    for item in saved:
        if item.get("id") != playlist_id:
            continue
        existing = list(item.get("track_ids", []))
        existing.extend(track_id for track_id in track_ids if track_id not in existing)
        item["track_ids"] = existing
        _save(saved)
        return
    raise ValueError("Playlist not found")


def remove_tracks(playlist_id: str, track_ids: list[str]) -> None:
    saved = _load()
    for item in saved:
        if item.get("id") != playlist_id:
            continue
        remove = set(track_ids)
        item["track_ids"] = [track_id for track_id in item.get("track_ids", []) if track_id not in remove]
        _save(saved)
        return
    raise ValueError("Playlist not found")


def delete(playlist_id: str) -> None:
    saved = _load()
    updated = [item for item in saved if item.get("id") != playlist_id]
    if len(updated) == len(saved):
        raise ValueError("Playlist not found")
    _save(updated)
