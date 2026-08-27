"""Jellyfin HTTP API client.

Implements the subset of the Jellyfin REST API needed by the listener:
authentication, library browsing (artists, albums, tracks), playlist
management, artwork retrieval, stream URLs and now-playing reporting.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

import requests

from .models import SOURCE_JELLYFIN, Album, Artist, Playlist, Track
from . import config as cfg

CLIENT_NAME = "Jellyfin Music Listener CLI"
APP_VERSION = "1.0.0"

AUDIO_FIELDS = "RunTimeTicks,ProductionYear,ParentId,AlbumId,Album,Artists,ArtistItems,AlbumArtist"


class JellyfinError(Exception):
    pass


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    if not re.match(r"^https?://", url):
        raise JellyfinError("Server URL must start with http:// or https://")
    return url


class JellyfinClient:
    def __init__(self, base_url: str, device_id: str = "") -> None:
        self.base_url = normalize_base_url(base_url)
        self.device_id = device_id or secrets.token_hex(8)
        self.token: str = ""
        self.user_id: str = ""
        self.username: str = ""
        self.server_name: str = ""
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self.album_cache: list[Album] | None = None

    # ------------------------------------------------------------------ auth

    def _auth_header(self, token: str = "") -> str:
        parts = [
            f'MediaBrowser Client="{CLIENT_NAME}"',
            f'Device="terminal"',
            f'DeviceId="{self.device_id}"',
            f'Version="{APP_VERSION}"',
        ]
        if token:
            parts.append(f'Token="{token}"')
        return ", ".join(parts)

    def authenticate(self, username: str, password: str) -> None:
        payload = {"Username": username, "Pw": password}
        headers = {
            "Content-Type": "application/json",
            "X-Emby-Authorization": self._auth_header(""),
        }
        try:
            resp = self._session.post(
                self.base_url + "/Users/AuthenticateByName",
                json=payload,
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as error:
            raise JellyfinError(f"Cannot reach server: {error}") from error
        if resp.status_code in (401, 400):
            detail = ""
            try:
                body = resp.json()
                detail = " ".join(
                    str(v) for v in body.values() if isinstance(v, str)
                )[:200]
            except ValueError:
                pass
            raise JellyfinError(
                f"Authentication failed (HTTP {resp.status_code})"
                + (f": {detail}" if detail else ": wrong user or password")
            )
        if resp.status_code != 200:
            raise JellyfinError(f"Authentication failed: HTTP {resp.status_code}")
        data = resp.json()
        token = data.get("AccessToken") or ""
        user = data.get("User") or {}
        if not token or not user.get("Id"):
            raise JellyfinError("Server returned an unexpected auth response")
        self.token = token
        self.user_id = user["Id"]
        self.username = user.get("Name", username)
        self.server_name = data.get("ServerId", "")

    def ensure_auth(self) -> None:
        if not self.token:
            raise JellyfinError("Not authenticated")

    def ping_public(self) -> dict[str, Any]:
        try:
            resp = self._session.get(
                self.base_url + "/System/Info/Public", timeout=10
            )
        except requests.RequestException as error:
            raise JellyfinError(f"Cannot reach server: {error}") from error
        if resp.status_code != 200:
            raise JellyfinError(f"Server responded with HTTP {resp.status_code}")
        return resp.json()

    # ------------------------------------------------------------------ http

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _headers(self) -> dict[str, str]:
        self.ensure_auth()
        return {
            "X-Emby-Token": self.token,
            "X-Emby-Authorization": self._auth_header(self.token),
        }

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._session.get(
                self._url(path),
                params=params,
                headers=self._headers(),
                timeout=30,
            )
        except requests.RequestException as error:
            raise JellyfinError(f"Request failed: {error}") from error
        if resp.status_code == 401:
            self.token = ""
            raise JellyfinError("Session expired or unauthorized")
        if resp.status_code != 200:
            raise JellyfinError(f"HTTP {resp.status_code} for {path}")
        return resp.json()

    def post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._session.post(
                self._url(path), params=params, headers=self._headers(), timeout=30
            )
        except requests.RequestException as error:
            raise JellyfinError(f"Request failed: {error}") from error
        if resp.status_code not in (200, 201, 204):
            raise JellyfinError(f"HTTP {resp.status_code} for {path}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def delete(self, path: str, params: dict[str, Any] | None = None) -> None:
        try:
            resp = self._session.delete(
                self._url(path), params=params, headers=self._headers(), timeout=30
            )
        except requests.RequestException as error:
            raise JellyfinError(f"Request failed: {error}") from error
        if resp.status_code not in (200, 204):
            raise JellyfinError(f"HTTP {resp.status_code} for {path}")

    # ---------------------------------------------------------------- browse

    def music_libraries(self) -> list[dict[str, str]]:
        data = self.get_json(
            f"/Users/{self.user_id}/Views",
            {"api_key": self.token},
        )
        libs = []
        for item in data.get("Items", []):
            if item.get("CollectionType", "").lower() == "music":
                libs.append({"id": item["Id"], "name": item.get("Name", "Music")})
        return libs

    def _music_library_items(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        libraries = self.music_libraries()
        if not libraries:
            return self.get_json("/Items", params).get("Items", [])
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for library in libraries:
            data = self.get_json("/Items", {**params, "parentId": library["id"]})
            for item in data.get("Items", []):
                item_id = item.get("Id", "")
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    items.append(item)
        return items

    def artists(self, limit: int = 10000) -> list[Artist]:
        params: dict[str, Any] = {
            "userId": self.user_id,
            "includeItemTypes": "MusicArtist",
            "sortBy": "SortName",
            "sortOrder": "Ascending",
            "recursive": "true",
            "fields": "AlbumCount,ChildCount",
            "limit": limit,
            "api_key": self.token,
        }
        items = self._music_library_items(params)
        if not items:
            data = self.get_json("/Artists", params)
            items = data.get("Items", [])
        out = []
        for item in items:
            out.append(
                Artist(
                    id=item["Id"],
                    name=item.get("Name", "?"),
                    album_count=item.get("AlbumCount") or item.get("ChildCount", 0) or 0,
                    source=SOURCE_JELLYFIN,
                )
            )
        return out

    def albums(self, artist_id: str | None = None) -> list[Album]:
        params: dict[str, Any] = {
            "userId": self.user_id,
            "recursive": "true",
            "includeItemTypes": "MusicAlbum",
            "sortBy": "SortName",
            "sortOrder": "Ascending",
            "fields": "ProductionYear,AlbumArtist,AlbumArtists,ArtistItems,ChildCount,ParentId",
            "enableTotalRecordCount": "false",
            "api_key": self.token,
        }
        if artist_id:
            data = self.get_json("/Items", {**params, "parentId": artist_id})
            items = data.get("Items", [])
            if not items:
                data = self.get_json("/Items", {**params, "albumArtistIds": artist_id})
                items = data.get("Items", [])
        else:
            items = self._music_library_items(params)
        artist_names = {artist.id: artist.name for artist in self.artists()}
        albums = []
        for item in items:
            artist = item.get("AlbumArtist", "")
            artists_list = item.get("AlbumArtists") or []
            if artists_list:
                artist = artist or artists_list[0].get("Name", "")
            artist = artist or artist_names.get(item.get("ParentId", ""), "")
            albums.append(
                Album(
                    id=item["Id"],
                    name=item.get("Name", "?"),
                    artist=artist,
                    year=item.get("ProductionYear"),
                    track_count=item.get("ChildCount", 0) or 0,
                    source=SOURCE_JELLYFIN,
                    cover_key=item["Id"],
                )
            )
        if artist_id is None:
            self.album_cache = albums
        return albums

    def _albums_by_id(self) -> dict[str, Album]:
        if self.album_cache is None:
            self.album_cache = self.albums()
        return {album.id: album for album in self.album_cache}

    def album_tracks(self, album_id: str) -> list[Track]:
        return self._audio_items(
            {"parentId": album_id, "sortBy": "ParentIndexNumber,IndexNumber,SortName"}
        )

    def all_tracks(self, limit: int = 5000) -> list[Track]:
        return self._audio_items(
            {"sortBy": "AlbumArtist,SortName", "limit": limit}
        )

    def search_tracks(self, query: str, limit: int = 200) -> list[Track]:
        return self._audio_items({"searchTerm": query, "limit": limit})

    def _audio_items(self, extra: dict[str, Any]) -> list[Track]:
        params: dict[str, Any] = {
            "userId": self.user_id,
            "recursive": "true",
            "includeItemTypes": "Audio",
            "fields": AUDIO_FIELDS,
            "enableTotalRecordCount": "false",
            "api_key": self.token,
        }
        params.update(extra)
        data = self.get_json("/Items", params)
        tracks = []
        albums = self._albums_by_id()
        for item in data.get("Items", []):
            ticks = item.get("RunTimeTicks") or 0
            artists_list = item.get("Artists") or []
            album_id = item.get("AlbumId") or item.get("ParentId") or ""
            album_info = albums.get(album_id)
            artist = (
                artists_list[0]
                if artists_list
                else ((item.get("ArtistItems") or [{}])[0].get("Name", ""))
                or item.get("AlbumArtist", "")
                or (album_info.artist if album_info else "")
            )
            tracks.append(
                Track(
                    id=item["Id"],
                    title=item.get("Name", "?"),
                    artist=artist or "?",
                    album=item.get("Album", "") or (album_info.name if album_info else ""),
                    album_id=album_id,
                    year=item.get("ProductionYear"),
                    track_number=item.get("IndexNumber"),
                    duration=ticks / 10_000_000.0 if ticks else None,
                    source=SOURCE_JELLYFIN,
                    stream_ref=item["Id"],
                    cover_key=item.get("AlbumId") or item.get("ParentId") or item["Id"],
                )
            )
        return tracks

    # ------------------------------------------------------------- playlists

    def playlists(self) -> list[Playlist]:
        data = self.get_json(
            "/Items",
            {
                "userId": self.user_id,
                "includeItemTypes": "Playlist",
                "recursive": "true",
                "sortBy": "SortName",
                "enableTotalRecordCount": "false",
                "api_key": self.token,
            },
        )
        out = []
        for item in data.get("Items", []):
            out.append(
                Playlist(
                    id=item["Id"],
                    name=item.get("Name", "?"),
                    track_count=item.get("ChildCount", 0) or 0,
                    source=SOURCE_JELLYFIN,
                )
            )
        return out

    def playlist_tracks(self, playlist_id: str) -> list[Track]:
        data = self.get_json(
            f"/Playlists/{playlist_id}/Items",
            {"userId": self.user_id, "api_key": self.token},
        )
        tracks = []
        for entry in data.get("Items", []):
            item = entry
            ticks = item.get("RunTimeTicks") or 0
            artists_list = item.get("Artists") or []
            artist = (
                artists_list[0]
                if artists_list
                else ((item.get("ArtistItems") or [{}])[0].get("Name", ""))
            )
            tracks.append(
                Track(
                    id=item["Id"],
                    title=item.get("Name", "?"),
                    artist=artist or "?",
                    album=item.get("Album", ""),
                    album_id=item.get("AlbumId", ""),
                    year=item.get("ProductionYear"),
                    duration=ticks / 10_000_000.0 if ticks else None,
                    source=SOURCE_JELLYFIN,
                    stream_ref=item["Id"],
                    cover_key=item.get("AlbumId") or item.get("ParentId") or item["Id"],
                    playlist_entry_id=entry.get("PlaylistItemId"),
                )
            )
        return tracks

    def create_playlist(self, name: str, item_ids: list[str] | None = None) -> str:
        params = {
            "Name": name,
            "userId": self.user_id,
            "api_key": self.token,
        }
        if item_ids:
            params["Ids"] = ",".join(item_ids)
        data = self.post("/Playlists", params)
        return data.get("Id", "")

    def add_to_playlist(self, playlist_id: str, item_ids: list[str]) -> None:
        self.post(
            f"/Playlists/{playlist_id}/Items",
            {"Ids": ",".join(item_ids), "userId": self.user_id},
        )

    def remove_from_playlist(self, playlist_id: str, entry_ids: list[str]) -> None:
        self.delete(
            f"/Playlists/{playlist_id}/Items",
            {"EntryIds": ",".join(entry_ids)},
        )

    def delete_playlist(self, playlist_id: str) -> None:
        self.delete(f"/Items/{playlist_id}")

    # ----------------------------------------------------------------- media

    def stream_url(self, item_id: str) -> str:
        return self._url(
            f"/Items/{item_id}/Download?api_key={self.token}&DeviceId={self.device_id}"
        )

    def image_url(self, item_id: str, max_width: int | None = None) -> str:
        url = self._url(f"/Items/{item_id}/Images/Primary?api_key={self.token}")
        if max_width:
            url += f"&maxWidth={max_width}&quality=95"
        return url

    def image_bytes(self, item_id: str, max_width: int | None = None) -> bytes | None:
        try:
            resp = self._session.get(
                self.image_url(item_id, max_width),
                headers={"X-Emby-Token": self.token},
                timeout=20,
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        return resp.content

    # ---------------------------------------------------------- now playing

    def report_start(self, item_id: str, position: float = 0.0) -> None:
        self._post_play(f"/Sessions/Playing", item_id, position)

    def report_progress(self, item_id: str, position: float) -> None:
        self._post_play("/Sessions/Playing/Progress", item_id, position)

    def report_stop(self, item_id: str, position: float) -> None:
        self._post_play("/Sessions/Playing/Stopped", item_id, position)

    def _post_play(self, path: str, item_id: str, position: float) -> None:
        if not self.token:
            return
        payload = {
            "ItemId": item_id,
            "CanSeek": True,
            "IsPaused": False,
            "IsMuted": False,
            "PositionTicks": int(max(position, 0) * 10_000_000),
            "PlayMethod": "DirectPlay",
        }
        try:
            self._session.post(
                self._url(path), json=payload, headers=self._headers(), timeout=10
            ).raise_for_status()
        except (requests.RequestException, JellyfinError):
            pass


def connect(server_url: str, username: str, password: str, device_id: str = "") -> JellyfinClient:
    client = JellyfinClient(server_url, device_id)
    client.ping_public()
    client.authenticate(username, password)
    return client
