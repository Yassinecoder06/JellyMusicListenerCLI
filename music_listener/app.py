"""Textual TUI application for the Jellyfin Music Listener CLI."""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

from . import config as cfg
from . import coverart
from . import localplaylists
from .jellyfin import JellyfinClient, JellyfinError, connect
from .locallib import LocalLibrary, find_cover
from .models import SOURCE_JELLYFIN, SOURCE_LOCAL, Album, Artist, Playlist, Track
from .player import MpvPlayer, PlayerStatus
from .widgets import CoverWidget, NowPlayingBar

LEVEL_TRACKS = "tracks"
LEVEL_ALBUMS = "albums"
LEVEL_ARTISTS = "artists"
LEVEL_PLAYLISTS = "playlists"
LEVEL_ALBUM_DETAIL = "album_detail"
LEVEL_ARTIST_DETAIL = "artist_detail"
LEVEL_PLAYLIST_DETAIL = "playlist_detail"
LEVEL_SEARCH = "search"

NAV_OPTIONS = [
    Option("🔎 Search", id="search"),
    Option("🎵 All Tracks", id=LEVEL_TRACKS),
    Option("💿 Albums", id=LEVEL_ALBUMS),
    Option("🎤 Artists", id=LEVEL_ARTISTS),
    Option("🎧 Playlists", id=LEVEL_PLAYLISTS),
    Option("", id="sep", disabled=True),
    Option("↔  Switch source (F2)", id="source"),
    Option("⟳  Rescan library", id="rescan"),
    Option("⚙  Server / Setup", id="setup"),
]

HELP_TEXT = """\
[bold]Playback[/bold]
  enter        play selected / open selected album · artist · playlist
  space        pause / resume                v  stop
  z / x        previous / next track         ← / →   seek ±10s
  + / -        volume                        m  mute

[bold]Queue modes[/bold]
  s            shuffle on/off                r  repeat off → all → one

[bold]Library[/bold]
  /            search tracks                 f5 refresh current view
  esc          back                          tab switch panel focus
  e            edit metadata (local tracks)

[bold]Playlists (local or server)[/bold]
  n            new empty playlist            a  add highlighted track/album
  d            remove highlighted entry from current playlist
  D            delete playlist (on the playlist list)

[bold]General[/bold]
  ?            this help                     q  quit
"""


class LibraryTable(DataTable):
    BINDINGS = [
        b
        for b in DataTable.BINDINGS
        if getattr(b, "key", None) not in ("left", "right")
    ] + [
        Binding("left", "app.seek_back", show=False),
        Binding("right", "app.seek_forward", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cursor_type = "row"
        self.show_cursor = True
        self.zebra_stripes = True


class DismissableScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel_screen", show=False)]

    def action_cancel_screen(self) -> None:
        self.dismiss(None)


class SetupScreen(DismissableScreen):
    def __init__(self, values: dict[str, str] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial = values or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-box"):
            yield Label("[b]Connect your music[/b]", id="setup-title")
            yield Label(
                "Save the server and/or a local folder — both can coexist.\n"
                "Switch between them anytime with F2 or ↔ Switch source.",
                id="setup-hint",
            )
            yield Input(
                value=self._initial.get("server", ""),
                placeholder="Jellyfin server URL  e.g. http://192.168.1.10:8096",
                id="setup-server",
            )
            yield Input(
                value=self._initial.get("user", ""),
                placeholder="Jellyfin username",
                id="setup-user",
            )
            yield Input(
                placeholder="Jellyfin password", password=True, id="setup-password"
            )
            yield Input(
                value=self._initial.get("folder", ""),
                placeholder="Local music folder  Artist/Album/ layout",
                id="setup-folder",
            )
            with Horizontal(id="setup-buttons"):
                yield Button("Save", variant="primary", id="setup-save")
                yield Button("Cancel", id="setup-cancel")

    @on(Input.Submitted, "#setup-server")
    @on(Input.Submitted, "#setup-user")
    @on(Input.Submitted, "#setup-password")
    @on(Input.Submitted, "#setup-folder")
    def submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#setup-save")
    def pressed_save(self) -> None:
        self._submit()

    @on(Button.Pressed, "#setup-cancel")
    def pressed_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        self.dismiss(
            {
                "server": self.query_one("#setup-server", Input).value.strip(),
                "user": self.query_one("#setup-user", Input).value.strip(),
                "password": self.query_one("#setup-password", Input).value.strip(),
                "folder": self.query_one("#setup-folder", Input).value.strip(),
            }
        )


class NameInputScreen(DismissableScreen):
    AUTO_FOCUS = "#name-input"

    def __init__(self, title: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="name-box"):
            yield Label(f"[b]{self._title}[/b]")
            yield Input(placeholder="Name", id="name-input")

    @on(Input.Submitted, "#name-input")
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class AddToPlaylistScreen(DismissableScreen):
    AUTO_FOCUS = "#add-list"

    def __init__(self, playlists: list[Playlist], **kwargs) -> None:
        super().__init__(**kwargs)
        self._playlists = playlists

    def compose(self) -> ComposeResult:
        options = [Option("< Create new playlist… >", id="__new__")]
        options += [
            Option(f"{p.name}  ({p.track_count} tracks)", id=p.id)
            for p in self._playlists
        ]
        with Vertical(id="add-box"):
            yield Label("[b]Add to playlist[/b]")
            yield OptionList(*options, id="add-list")

    @on(OptionList.OptionSelected, "#add-list")
    def selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option.id or "")
        if option_id == "__new__":
            self.dismiss(("new", ""))
        else:
            self.dismiss(("existing", option_id))


class ConfirmScreen(DismissableScreen):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="error", id="confirm-yes")
                yield Button("No", id="confirm-no")

    @on(Button.Pressed, "#confirm-yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def no(self) -> None:
        self.dismiss(False)


class EditMetadataScreen(DismissableScreen):
    def __init__(self, track, **kwargs) -> None:
        super().__init__(**kwargs)
        self.track = track

    def compose(self) -> ComposeResult:
        t = self.track
        with Vertical(id="edit-box"):
            yield Label("[b]Edit Metadata[/b]")
            yield Label("Artist", classes="section-title")
            yield Input(value=t.artist or "", id="edit-artist")
            yield Label("Album Artist", classes="section-title")
            yield Input(value=getattr(t, "album_artist", "") or t.artist or "", id="edit-albumartist")
            yield Label("Album", classes="section-title")
            yield Input(value=t.album or "", id="edit-album")
            yield Label("Title", classes="section-title")
            yield Input(value=t.title or "", id="edit-title")
            with Horizontal():
                yield Input(value=str(t.track_number or 1), placeholder="Track", id="edit-track")
                yield Input(value=str(getattr(t, "disc_number", 1) or 1), placeholder="Disc", id="edit-disc")
                yield Input(value=str(t.year or ""), placeholder="Year", id="edit-year")
            yield Label("Genre", classes="section-title")
            yield Input(value=getattr(t, "genre", "") or "", placeholder="Genre", id="edit-genre")
            with Horizontal(id="edit-buttons"):
                yield Button("Save", variant="primary", id="edit-save")
                yield Button("Cancel", id="edit-cancel")
            yield Static("Advanced MBIDs shown read-only. After save, file tags and path will be updated.", classes="help")
            if getattr(t, "mb_recording_id", None):
                yield Static(f"MB Recording: {t.mb_recording_id}", classes="help")

    @on(Button.Pressed, "#edit-save")
    def save(self) -> None:
        data = {
            "artist": self.query_one("#edit-artist", Input).value.strip(),
            "album_artist": self.query_one("#edit-albumartist", Input).value.strip(),
            "album": self.query_one("#edit-album", Input).value.strip(),
            "title": self.query_one("#edit-title", Input).value.strip(),
            "track_number": self.query_one("#edit-track", Input).value.strip(),
            "disc_number": self.query_one("#edit-disc", Input).value.strip(),
            "year": self.query_one("#edit-year", Input).value.strip(),
            "genre": self.query_one("#edit-genre", Input).value.strip(),
        }
        self.dismiss(data)

    @on(Button.Pressed, "#edit-cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen):
    AUTO_FOCUS = None
    BINDINGS = [
        Binding("escape", "close_help", show=False),
        Binding("q", "close_help", show=False),
        Binding("question_mark", "close_help", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(HELP_TEXT, id="help-text")
            yield Label("[dim]esc closes[/dim]", id="help-footer")

    def action_close_help(self) -> None:
        self.dismiss(None)


class ListenerApp(App):
    TITLE = "Jellyfin Music Listener CLI"

    CSS = """
    #topbar {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    #main { height: 1fr; }
    #sidebar {
        width: 32;
        min-width: 24;
        border-right: solid $primary;
        padding-right: 1;
    }
    #table { width: 1fr; height: 1fr; }
    #searchbox { display: none; }
    #searchbox.visible { display: block; }
    #np {
        height: auto;
        border-top: solid $primary;
        padding: 0 1;
    }
    #np-cover {
        width: 38;
        height: 19;
        margin-right: 1;
        padding-top: 0;
    }
    #np-info {
        width: 1fr;
        min-width: 30;
        max-width: 100%;
        height: auto;
        padding-top: 1;
    }
    #np-title { text-style: bold; }
    #np-sub { color: $text-muted; }
    #np-progress { margin-top: 1; color: $accent; }
    #np-side {
        width: 22;
        max-width: 30%;
        height: auto;
        padding-top: 2;
        color: $text-muted;
        align-horizontal: right;
    }
    #np-flags { margin-top: 1; }

    #setup-box, #name-box, #add-box, #confirm-box, #help-box, #edit-box {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 72;
        margin: 1 2;
    }
    #help-box { width: 66; }
    #edit-box { width: 78; }
    #setup-hint { color: $text-muted; margin-bottom: 1; }
    #setup-box Input, #edit-box Input { margin-bottom: 1; }
    #setup-buttons, #confirm-buttons, #edit-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: center;
    }
    #setup-buttons Button, #confirm-buttons Button, #edit-buttons Button { margin: 0 1; }
    #add-list { height: auto; max-height: 16; border-top: solid $boost; }
    #help-text { padding-bottom: 1; }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "Pause"),
        Binding("/", "open_search", "Search"),
        Binding("q", "quit_app", "Quit"),
        Binding("v", "stop_playback", "Stop", show=False),
        Binding("z", "prev_track", "Prev", show=False),
        Binding("x", "next_track", "Next", show=False),
        Binding("comma", "seek_back", "Seek-", show=False),
        Binding("full_stop", "seek_forward", "Seek+", show=False),
        Binding("plus", "volume_up", "Vol+", show=False),
        Binding("equal", "volume_up", "Vol+", show=False),
        Binding("minus", "volume_down", "Vol-", show=False),
        Binding("underscore", "volume_down", "Vol-", show=False),
        Binding("m", "toggle_mute", "Mute", show=False),
        Binding("s", "toggle_shuffle", "Shuffle", show=False),
        Binding("r", "cycle_repeat", "Repeat", show=False),
        Binding("n", "new_playlist", "New playlist"),
        Binding("a", "add_to_playlist", "Add to playlist"),
        Binding("e", "edit_metadata", "Edit", show=False),
        Binding("d", "remove_entry", "Remove", show=False),
        Binding("D", "delete_playlist", "Delete playlist", show=False),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("f2", "switch_source", "Source"),
        Binding("question_mark", "help_keys", "Help"),
        Binding("escape", "go_back", "Back", show=False),
    ]

    def __init__(
        self,
        force_setup: bool = False,
        local_root: str | None = None,
        server_override: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.force_setup = force_setup
        base = cfg.load_config()
        if server_override:
            base.server_url = server_override
        if local_root:
            base.music_folder = local_root
            base.active_source = SOURCE_LOCAL
        self.config = base

        self.client: JellyfinClient | None = None
        self.local_lib: LocalLibrary | None = None
        self.local_snapshot = None
        self.player: MpvPlayer | None = None

        self.stack: list[tuple[str, dict[str, Any]]] = [(LEVEL_ALBUMS, {})]
        self.rows: list[Any] = []
        self.current_level = LEVEL_ALBUMS

        self.queue: list[Track] = []
        self.order: list[int] = []
        self.q_pos: int = -1
        self.current_track: Track | None = None

        self._last_report_time = 0.0
        self._pending_target: tuple[str, str] | None = None

    # ------------------------------------------------------------- utilities

    def _persist(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self.config, key, value)
        cfg.save_config(self.config)

    def now_playing_bar(self) -> NowPlayingBar:
        return self.query_one("#np", NowPlayingBar)

    @property
    def active_source(self) -> str:
        return self.config.active_source

    def refresh_soon(self) -> None:
        self.call_after_refresh(self._update_topbar)

    def _update_topbar(self) -> None:
        bar = self.query_one("#topbar", Static)
        source = "SERVER" if self.active_source == SOURCE_JELLYFIN else "LOCAL"
        detail = ""
        if self.active_source == SOURCE_JELLYFIN and self.client:
            detail = f"{self.client.base_url}  ·  {self.client.username}"
        elif self.active_source == SOURCE_LOCAL:
            if self.local_snapshot is not None:
                detail = (
                    f"{len(self.local_snapshot.artists)} artists · "
                    f"{len(self.local_snapshot.albums)} albums · "
                    f"{len(self.local_snapshot.tracks)} tracks"
                )
            elif self.config.resolved_music_folder():
                detail = self.config.resolved_music_folder()
        bar.update(f"♪ {self.TITLE}  │  [{source}] {detail}")

    # ------------------------------------------------------------- lifecycle

    def compose(self) -> ComposeResult:
        cover_cols, cover_rows = self._cover_geometry(
            self.size.width or 110, self.size.height or 36
        )
        yield Static("", id="topbar")
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield OptionList(*NAV_OPTIONS, id="nav")
            yield LibraryTable(id="table")
        yield Input(
            placeholder="Search tracks — Enter to search, Esc to cancel",
            id="searchbox",
        )
        yield NowPlayingBar(cover_cols=cover_cols, cover_rows=cover_rows, id="np")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#table", LibraryTable).add_columns("Loading…")
        self.query_one("#nav", OptionList).focus()
        self._last_term_size = None
        self._apply_cover_geometry()
        self._last_term_size = (self.size.width, self.size.height)
        try:
            self.player = MpvPlayer(
                on_end_of_track=lambda: self.call_from_thread(self.auto_advance)
            )
            self.player.set_volume(self.config.volume)
            if status := self.player.ssh_audio_status:
                self.notify(status, severity="warning" if "unavailable" in status else "information", timeout=10)
        except Exception as error:
            self.notify(
                f"Audio engine unavailable: {error}", severity="error", timeout=12
            )
            self.player = None
        self.set_interval(0.5, self.tick)
        self.bootstrap()

    @staticmethod
    def _cover_geometry(width: int, height: int = 40) -> tuple[int, int]:
        if width >= 130:
            cols, rows = (48, 24)
        elif width >= 100:
            cols, rows = (38, 19)
        elif width >= 78:
            cols, rows = (30, 15)
        else:
            cols, rows = (24, 12)
        row_cap = max((height - 10) // 2, 10)
        if rows > row_cap:
            rows = row_cap
            cols = min(cols, rows * 2)
        return (max(cols, 16), max(rows, 8))

    def _apply_cover_geometry(self) -> None:
        cols, rows = self._cover_geometry(
            self.size.width or 110, self.size.height or 36
        )
        cover = self.query_one("#np-cover", CoverWidget)
        changed = (cover.cols, cover.rows) != (cols, rows)
        cover.apply_size(cols, rows)
        if changed and self.current_track is not None:
            self.load_cover_for(self.current_track)

    @work(thread=True, group="boot", exclusive=True)
    def bootstrap(self) -> None:
        if self.force_setup:
            self.call_from_thread(self.action_open_setup)
        url = self.config.resolved_server()
        user = self.config.resolved_username()
        password = cfg.get_password()
        if url and user:
            try:
                client = connect(url, user, password, self.config.device_id)
            except JellyfinError:
                client = None
                self.call_from_thread(
                    self.notify,
                    f"Could not connect to {url}; check Server / Setup.",
                    severity="warning",
                    timeout=10,
                )
            else:
                self.call_from_thread(self._set_client, client)
                return
        folder = self.config.resolved_music_folder()
        if folder and Path(folder).is_dir():
            self.call_from_thread(self._startup_local)
            return
        if not self.force_setup:
            initial = {
                "server": url,
                "user": user,
                "folder": folder,
            }
            self.call_from_thread(self.open_setup_with, initial)

    def open_setup_with(self, initial: dict[str, str]) -> None:
        def handle(result: Any) -> None:
            if isinstance(result, dict):
                self.apply_setup(result)

        self.push_screen(SetupScreen(initial), handle)

    def action_open_setup(self) -> None:
        def handle(result: Any) -> None:
            if isinstance(result, dict):
                self.apply_setup(result)

        self.push_screen(SetupScreen(), handle)

    @work(thread=True, group="connect", exclusive=True)
    def try_connect(self, url: str, user: str, password: str) -> None:
        try:
            client = connect(url, user, password, self.config.device_id)
        except JellyfinError as error:
            self.call_from_thread(
                self.notify,
                f"Connection failed: {error}",
                severity="error",
                timeout=10,
            )
            return
        storage = cfg.save_password(password)
        self._persist(server_url=url, username=user, active_source=SOURCE_JELLYFIN)
        self.call_from_thread(self._set_client, client)
        if storage == "dotenv":
            self.call_from_thread(
                self.notify,
                "Keyring unavailable; password saved in protected .env fallback.",
                severity="warning",
                timeout=12,
            )
        elif not storage:
            self.call_from_thread(
                self.notify,
                "Connected, but the password could not be persisted.",
                severity="warning",
                timeout=12,
            )

    def _set_client(self, client: JellyfinClient) -> None:
        self.client = client
        self._persist(active_source=SOURCE_JELLYFIN)
        self.notify(f"Connected to {client.base_url}", timeout=4)
        self.stack = [(LEVEL_ALBUMS, {})]
        self.reload_current()

    def _startup_local(self) -> None:
        self._persist(active_source=SOURCE_LOCAL)
        self.stack = [(LEVEL_ALBUMS, {})]
        self.reload_current()

    @work(thread=True, group="applysetup", exclusive=True)
    def apply_setup(self, values: dict[str, str]) -> None:
        server = values.get("server", "")
        user = values.get("user", "")
        password = values.get("password", "")
        folder = values.get("folder", "")
        if server and user and password:
            try:
                client = connect(server, user, password, self.config.device_id)
            except JellyfinError as error:
                self.call_from_thread(
                    self.notify,
                    f"Connection failed: {error}",
                    severity="error",
                    timeout=10,
                )
                return
            storage = cfg.save_password(password)
            self._persist(
                server_url=server,
                username=user,
                active_source=SOURCE_JELLYFIN,
                music_folder=folder or self.config.music_folder,
            )
            self.call_from_thread(self._set_client, client)
            if storage == "dotenv":
                self.call_from_thread(
                    self.notify,
                    "Keyring unavailable; password saved in protected .env fallback.",
                    severity="warning",
                    timeout=10,
                )
            elif not storage:
                self.call_from_thread(
                    self.notify,
                    "Password could not be persisted.",
                    severity="warning",
                    timeout=10,
                )
            return
        if folder:
            expanded = str(Path(folder).expanduser())
            if not Path(expanded).is_dir():
                self.call_from_thread(
                    self.notify, f"Not a folder: {expanded}", severity="error"
                )
                return
            self._persist(music_folder=expanded, active_source=SOURCE_LOCAL)
            self.local_lib = None
            self.local_snapshot = None
            self.call_from_thread(self._startup_local)
            return
        self.call_from_thread(
            self.notify,
            "Nothing saved: fill server + credentials, or a local folder.",
            severity="warning",
        )

    # ---------------------------------------------------------------- browse

    @work(thread=True, group="nav", exclusive=True)
    def load_level(self, level: str, context: dict[str, Any]) -> None:
        try:
            rows, kind, title = self.fetch_level(level, context)
        except Exception as error:
            self.call_from_thread(
                self.notify, str(error), severity="error", timeout=8
            )
            return
        self.call_from_thread(self.fill_table, level, rows, kind, title)

    def fetch_level(self, level: str, context: dict[str, Any]):
        if self.active_source == SOURCE_JELLYFIN:
            if self.client is None:
                raise RuntimeError("Not connected to a Jellyfin server yet")
            if level == LEVEL_ALBUMS:
                return self.client.albums(), "album", "Albums"
            if level == LEVEL_ARTISTS:
                return self.client.artists(), "artist", "Artists"
            if level == LEVEL_TRACKS:
                return self.client.all_tracks(), "track", "All Tracks"
            if level == LEVEL_SEARCH:
                query = context.get("query", "")
                return (
                    self.client.search_tracks(query),
                    "track",
                    f"Search: {query}",
                )
            if level == LEVEL_PLAYLISTS:
                return self.client.playlists(), "playlist", "Playlists"
            if level == LEVEL_ALBUM_DETAIL:
                album: Album = context["album"]
                return self.client.album_tracks(album.id), "track", album.name
            if level == LEVEL_ARTIST_DETAIL:
                artist: Artist = context["artist"]
                albums = self.client.albums(artist_id=artist.id)
                if not albums:
                    raise RuntimeError(f"No albums found for {artist.name}")
                return albums, "album", artist.name
            if level == LEVEL_PLAYLIST_DETAIL:
                playlist: Playlist = context["playlist"]
                return (
                    self.client.playlist_tracks(playlist.id),
                    "track",
                    playlist.name,
                )
            raise RuntimeError(f"Unknown view: {level}")

        snapshot = self.get_local_snapshot()
        if level == LEVEL_ALBUMS:
            return snapshot.albums, "album", "Albums"
        if level == LEVEL_ARTISTS:
            return snapshot.artists, "artist", "Artists"
        if level == LEVEL_TRACKS:
            return snapshot.tracks, "track", "All Tracks"
        if level == LEVEL_SEARCH:
            query = context.get("query", "").lower()
            matched = [
                t
                for t in snapshot.tracks
                if query in t.title.lower()
                or query in t.artist.lower()
                or query in t.album.lower()
            ]
            return matched, "track", f"Search: {context.get('query', '')}"
        if level == LEVEL_PLAYLISTS or level == LEVEL_PLAYLIST_DETAIL:
            playlists = localplaylists.playlists(snapshot.tracks)
            if level == LEVEL_PLAYLISTS:
                return playlists, "playlist", "Playlists"
            playlist: Playlist = context["playlist"]
            return localplaylists.tracks(playlist.id, snapshot.tracks), "track", playlist.name
        if level == LEVEL_ALBUM_DETAIL:
            album: Album = context["album"]
            matched = [t for t in snapshot.tracks if t.album_id == album.id]
            return matched, "track", album.name
        if level == LEVEL_ARTIST_DETAIL:
            artist: Artist = context["artist"]
            matched = [
                a for a in snapshot.albums if a.artist.lower() == artist.name.lower()
            ]
            return matched, "album", artist.name
        raise RuntimeError(f"Unknown view: {level}")

    def fill_table(self, level: str, rows: list[Any], kind: str, title: str) -> None:
        table = self.query_one("#table", LibraryTable)
        self.current_level = level
        self.rows = rows
        table.clear(columns=True)
        if kind == "track":
            columns = ["#", "Title", "Artist", "Album", "Time"]
        elif kind == "album":
            columns = ["Album", "Artist", "Year", "Tracks"]
        elif kind == "artist":
            columns = ["Artist", "Albums"]
        else:
            columns = ["Playlist", "Tracks"]
        table.add_columns(*columns)
        for index, row in enumerate(rows, start=1):
            if isinstance(row, Track):
                number = "" if row.track_number is None else str(row.track_number)
                table.add_row(
                    number,
                    Text(row.title),
                    Text(row.artist),
                    Text(row.album),
                    row.duration_text,
                )
            elif isinstance(row, Album):
                year = "" if row.year is None else str(row.year)
                table.add_row(
                    Text(row.name), Text(row.artist), year, str(row.track_count)
                )
            elif isinstance(row, Artist):
                table.add_row(Text(row.name), str(row.album_count))
            elif isinstance(row, Playlist):
                table.add_row(Text(row.name), str(row.track_count))
        table.move_cursor(row=0, column=0)
        self._update_topbar()

    def reload_current(self) -> None:
        level, context = self.stack[-1]
        self.load_level(level, context)

    def push_view(self, level: str, context: dict[str, Any] | None = None) -> None:
        frame = (level, context or {})
        self.stack.append(frame)
        self.load_level(level, frame[1])

    def get_local_snapshot(self):
        if self.local_snapshot is None:
            root = self.config.resolved_music_folder()
            if not root:
                raise RuntimeError("No local music folder configured")
            self.local_lib = LocalLibrary(root)
            self.local_snapshot = self.local_lib.scan()
        return self.local_snapshot

    # ------------------------------------------------------------ nav events

    @on(OptionList.OptionSelected, "#nav")
    def nav_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option.id or "")
        if not option_id:
            return
        if option_id == "search":
            self.action_open_search()
        elif option_id == "source":
            self.action_switch_source()
        elif option_id == "rescan":
            self.action_rescan()
        elif option_id == "setup":
            self.action_open_setup()
        else:
            self.stack = [(option_id, {})]
            self.reload_current()

    @on(DataTable.RowSelected, "#table")
    def table_row_activated(self, event: DataTable.RowSelected) -> None:
        index = event.control.cursor_row
        if index < 0 or index >= len(self.rows):
            return
        row = self.rows[index]
        if isinstance(row, Track):
            self.play_context(index)
        elif isinstance(row, Album):
            self.push_view(LEVEL_ALBUM_DETAIL, {"album": row})
        elif isinstance(row, Artist):
            self.push_view(LEVEL_ARTIST_DETAIL, {"artist": row})
        elif isinstance(row, Playlist):
            self.push_view(LEVEL_PLAYLIST_DETAIL, {"playlist": row})

    # -------------------------------------------------------------- playback

    def play_context(self, start_index: int) -> None:
        track_positions = [
            i for i, r in enumerate(self.rows) if isinstance(r, Track)
        ]
        if start_index not in track_positions:
            return
        chosen = track_positions.index(start_index)
        tracks = [r for r in self.rows if isinstance(r, Track)]
        if not tracks:
            return
        self.queue = tracks
        if self.config.shuffle:
            rest = [i for i in range(len(tracks)) if i != chosen]
            random.shuffle(rest)
            self.order = [chosen] + rest
            self.q_pos = 0
        else:
            self.order = list(range(len(tracks)))
            self.q_pos = chosen
        self.start_current()

    def start_current(self) -> None:
        if self.player is None:
            self.notify("No audio engine available", severity="error")
            return
        if not self.queue or not self.order or not (0 <= self.q_pos < len(self.order)):
            return
        track = self.queue[self.order[self.q_pos]]
        self.current_track = track
        if track.source == SOURCE_LOCAL:
            ref = track.stream_ref
        else:
            if self.client is None:
                self.notify(
                    "Connect to the Jellyfin server first", severity="error"
                )
                return
            ref = self.client.stream_url(track.id)
        try:
            self.player.play(ref)
        except Exception as error:
            self.notify(str(error), severity="error")
            return
        self.now_playing_bar().set_track(track)
        self.load_cover_for(track)
        if track.source == SOURCE_JELLYFIN and self.client:
            self.client.report_start(track.id)
        self._last_report_time = time.monotonic()
        self.now_playing_bar().set_flags(
            self.player.get_volume(), None, self.config.shuffle, self.config.repeat
        )

    def auto_advance(self) -> None:
        if not self.queue:
            return
        if self.config.repeat == cfg.REPEAT_ONE:
            self.start_current()
            return
        self.q_pos += 1
        if self.q_pos >= len(self.order):
            if self.config.repeat == cfg.REPEAT_ALL:
                self.q_pos = 0
            else:
                self.q_pos = len(self.order) - 1
                if self.player:
                    self.player.stop()
                self.current_track = None
                self.now_playing_bar().set_track(None)
                self.now_playing_bar().update_status("stopped", None, None)
                return
        self.start_current()

    @work(thread=True, group="cover", exclusive=True)
    def load_cover_for(self, track: Track) -> None:
        data: bytes | None = None
        if track.source == SOURCE_LOCAL:
            album_dir = Path(track.stream_ref).parent
            cover_path = find_cover(album_dir)
            if cover_path and cover_path.is_file():
                try:
                    data = cover_path.read_bytes()
                except OSError:
                    data = None
        else:
            client = self.client
            if client is not None and track.cover_key:
                data = coverart.load_source_bytes(
                    f"jf:{track.cover_key}", lambda c=client, k=track.cover_key: c.image_bytes(k)
                )
        self.call_from_thread(self.now_playing_bar().set_cover_data, data)

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        size_key = (self.size.width, self.size.height)
        if size_key != getattr(self, "_last_term_size", None):
            self._last_term_size = size_key
            self._apply_cover_geometry()
        status = PlayerStatus()
        if self.player is not None:
            status = self.player.status()
        duration = status.duration
        if (not duration or duration <= 0) and self.current_track:
            duration = self.current_track.duration
        bar = self.now_playing_bar()
        bar.update_status(status.state, status.position, duration)
        bar.set_flags(
            status.volume if status.volume is not None else self.config.volume,
            status.muted,
            self.config.shuffle,
            self.config.repeat,
        )
        if (
            status.playing
            and self.current_track is not None
            and self.current_track.source == SOURCE_JELLYFIN
            and self.client
            and status.position is not None
            and time.monotonic() - self._last_report_time >= 10
        ):
            self._last_report_time = time.monotonic()
            self.client.report_progress(self.current_track.id, status.position)

    # --------------------------------------------------------------- actions

    def action_toggle_pause(self) -> None:
        if self.player is None:
            return
        was_paused = self.player.toggle_pause()
        if (
            not was_paused
            and self.current_track
            and self.current_track.source == SOURCE_JELLYFIN
            and self.client
        ):
            position = self.player.status().position or 0.0
            self.client.report_progress(self.current_track.id, position)

    def action_stop_playback(self) -> None:
        if self.player is None:
            return
        if (
            self.current_track
            and self.current_track.source == SOURCE_JELLYFIN
            and self.client
        ):
            position = self.player.status().position or 0.0
            self.client.report_stop(self.current_track.id, position)
        self.player.stop()
        self.now_playing_bar().update_status("stopped", None, None)

    def action_next_track(self) -> None:
        if not self.queue:
            return
        self.q_pos = (self.q_pos + 1) % len(self.order)
        self.start_current()

    def action_prev_track(self) -> None:
        if not self.queue:
            return
        if self.player is not None:
            position = self.player.status().position
            if position and position > 3:
                self.player.seek_to(0)
                return
        self.q_pos = (self.q_pos - 1) % len(self.order)
        self.start_current()

    def action_seek_back(self) -> None:
        if self.player is not None:
            self.player.seek(-10)

    def action_seek_forward(self) -> None:
        if self.player is not None:
            self.player.seek(10)

    def action_volume_up(self) -> None:
        self._change_volume(+5)

    def action_volume_down(self) -> None:
        self._change_volume(-5)

    def _change_volume(self, delta: float) -> None:
        if self.player is None:
            return
        volume = max(0.0, min(self.player.get_volume() + delta, 130.0))
        self.player.set_volume(volume)
        self._persist(volume=volume)

    def action_toggle_mute(self) -> None:
        if self.player is not None:
            self.player.toggle_mute()

    def action_toggle_shuffle(self) -> None:
        shuffle = not self.config.shuffle
        self._persist(shuffle=shuffle)
        if self.queue:
            current_index = (
                self.order[self.q_pos]
                if 0 <= self.q_pos < len(self.order) and self.order
                else 0
            )
            if shuffle:
                rest = [i for i in range(len(self.queue)) if i != current_index]
                random.shuffle(rest)
                self.order = [current_index] + rest
                self.q_pos = 0
            else:
                self.order = list(range(len(self.queue)))
                self.q_pos = current_index
        self.notify(f"Shuffle {'on' if shuffle else 'off'}", timeout=2)

    def action_cycle_repeat(self) -> None:
        cycle = {
            cfg.REPEAT_OFF: cfg.REPEAT_ALL,
            cfg.REPEAT_ALL: cfg.REPEAT_ONE,
            cfg.REPEAT_ONE: cfg.REPEAT_OFF,
        }
        repeat = cycle.get(self.config.repeat, cfg.REPEAT_OFF)
        self._persist(repeat=repeat)
        labels = {
            cfg.REPEAT_OFF: "off",
            cfg.REPEAT_ALL: "all",
            cfg.REPEAT_ONE: "one",
        }
        self.notify(f"Repeat: {labels.get(repeat, repeat)}", timeout=2)

    # ---------------------------------------------------------------- search

    def action_open_search(self) -> None:
        box = self.query_one("#searchbox", Input)
        box.add_class("visible")
        box.focus()

    @on(Input.Submitted, "#searchbox")
    def search_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        box = self.query_one("#searchbox", Input)
        box.remove_class("visible")
        self.query_one("#table", LibraryTable).focus()
        if query:
            self.push_view(LEVEL_SEARCH, {"query": query})

    # ------------------------------------------------------------- playlists

    def resolve_add_target(self) -> tuple[str, str] | None:
        index = self.query_one("#table", LibraryTable).cursor_row
        if index < 0 or index >= len(self.rows):
            return None
        row = self.rows[index]
        if isinstance(row, Track):
            return ("ids", row.id)
        if isinstance(row, Album):
            return ("album", row.id)
        return None

    def action_add_to_playlist(self) -> None:
        if self.active_source == SOURCE_JELLYFIN and self.client is None:
            self.notify(
                "Playlists need a Jellyfin server connection",
                severity="warning",
                timeout=5,
            )
            return
        target = self.resolve_add_target()
        if target is None:
            self.notify("Highlight a track or an album first", timeout=4)
            return
        self.fetch_playlists_then_add(target)

    @work(thread=True, group="plfetch", exclusive=True)
    def fetch_playlists_then_add(self, target: tuple[str, str]) -> None:
        try:
            if self.active_source == SOURCE_LOCAL:
                playlists = localplaylists.playlists(self.get_local_snapshot().tracks)
            elif self.client is not None:
                playlists = self.client.playlists()
            else:
                return
        except (JellyfinError, ValueError) as error:
            self.call_from_thread(
                self.notify, str(error), severity="error", timeout=8
            )
            return
        self._pending_target = target

        def handle(result: Any) -> None:
            if result is None:
                return
            mode, value = result
            if mode == "new":
                self.push_screen(
                    NameInputScreen("Name the new playlist"),
                    lambda name: name and self.perform_add("new", name),
                )
            else:
                self.perform_add("existing", value)

        self.call_from_thread(self.push_screen, AddToPlaylistScreen(playlists), handle)

    @work(thread=True, group="plcreate", exclusive=True)
    def create_playlist_worker(self, name: str, item_ids: list[str] | None = None) -> None:
        try:
            if self.active_source == SOURCE_LOCAL:
                localplaylists.create(name, item_ids)
            elif self.client is not None:
                self.client.create_playlist(name, item_ids or [])
            else:
                return
        except (JellyfinError, ValueError) as error:
            self.call_from_thread(
                self.notify,
                f"Could not create playlist: {error}",
                severity="error",
                timeout=8,
            )
            return
        message = f"Created playlist '{name}'"
        if item_ids:
            message += f" with {len(item_ids)} tracks"
        self.call_from_thread(self.notify, message, timeout=4)
        if self.current_level in (LEVEL_PLAYLISTS, LEVEL_PLAYLIST_DETAIL):
            self.call_from_thread(self.reload_current)

    @work(thread=True, group="pladd", exclusive=True)
    def perform_add(self, destination: str, name_or_id: str) -> None:
        target = self._pending_target
        self._pending_target = None
        if target is None:
            return
        kind, value = target
        try:
            if self.active_source == SOURCE_LOCAL:
                snapshot = self.get_local_snapshot()
                ids = [track.id for track in snapshot.tracks if track.album_id == value] if kind == "album" else [value]
                if destination == "new":
                    localplaylists.create(name_or_id, ids)
                    message = f"Created playlist '{name_or_id}' with {len(ids)} tracks"
                else:
                    localplaylists.add_tracks(name_or_id, ids)
                    message = f"Added {len(ids)} track(s) to playlist"
            elif self.client is not None and kind == "album":
                ids = [t.id for t in self.client.album_tracks(value)]
                if destination == "new":
                    self.client.create_playlist(name_or_id, ids)
                    message = f"Created playlist '{name_or_id}' with {len(ids)} tracks"
                else:
                    self.client.add_to_playlist(name_or_id, ids)
                    message = f"Added {len(ids)} track(s) to playlist"
            elif self.client is not None:
                ids = [value]
                if destination == "new":
                    self.client.create_playlist(name_or_id, ids)
                    message = f"Created playlist '{name_or_id}' with {len(ids)} tracks"
                else:
                    self.client.add_to_playlist(name_or_id, ids)
                    message = f"Added {len(ids)} track(s) to playlist"
            else:
                return
        except (JellyfinError, ValueError) as error:
            self.call_from_thread(
                self.notify, f"Add failed: {error}", severity="error", timeout=8
            )
            return
        self.call_from_thread(self.notify, message, timeout=4)
        if self.current_level in (LEVEL_PLAYLISTS, LEVEL_PLAYLIST_DETAIL):
            self.call_from_thread(self.reload_current)

    def action_new_playlist(self) -> None:
        self.push_screen(
            NameInputScreen("Create a new playlist"),
            lambda name: name and self.create_playlist_worker(name),
        )

    def action_remove_entry(self) -> None:
        if self.current_level != LEVEL_PLAYLIST_DETAIL:
            return
        index = self.query_one("#table", LibraryTable).cursor_row
        if index < 0 or index >= len(self.rows):
            return
        track = self.rows[index]
        if not isinstance(track, Track) or not track.playlist_entry_id:
            return
        context = self.stack[-1][1]
        playlist: Playlist = context["playlist"]
        self.remove_entries_worker(playlist.id, [track.playlist_entry_id])

    @work(thread=True, group="plremove", exclusive=True)
    def remove_entries_worker(self, playlist_id: str, entry_ids: list[str]) -> None:
        try:
            if self.active_source == SOURCE_LOCAL:
                localplaylists.remove_tracks(playlist_id, entry_ids)
            elif self.client is not None:
                self.client.remove_from_playlist(playlist_id, entry_ids)
            else:
                return
        except (JellyfinError, ValueError) as error:
            self.call_from_thread(
                self.notify, f"Remove failed: {error}", severity="error", timeout=8
            )
            return
        self.call_from_thread(self.notify, "Removed from playlist", timeout=3)
        self.call_from_thread(self.reload_current)

    def action_delete_playlist(self) -> None:
        if self.current_level != LEVEL_PLAYLISTS:
            return
        index = self.query_one("#table", LibraryTable).cursor_row
        if index < 0 or index >= len(self.rows):
            return
        playlist = self.rows[index]
        if not isinstance(playlist, Playlist):
            return

        def handle(result: Any) -> None:
            if result:
                self.delete_playlist_worker(playlist.id, playlist.name)

        self.push_screen(ConfirmScreen(f"Delete playlist '{playlist.name}'?"), handle)

    @work(thread=True, group="pldelete", exclusive=True)
    def delete_playlist_worker(self, playlist_id: str, name: str) -> None:
        try:
            if self.active_source == SOURCE_LOCAL:
                localplaylists.delete(playlist_id)
            elif self.client is not None:
                self.client.delete_playlist(playlist_id)
            else:
                return
        except (JellyfinError, ValueError) as error:
            self.call_from_thread(
                self.notify, f"Delete failed: {error}", severity="error", timeout=8
            )
            return
        self.call_from_thread(self.notify, f"Deleted playlist '{name}'", timeout=3)
        self.call_from_thread(self.reload_current)

    def action_edit_metadata(self) -> None:
        index = self.query_one("#table", LibraryTable).cursor_row
        if index < 0 or index >= len(self.rows):
            self.notify("Highlight a track first", timeout=3)
            return
        row = self.rows[index]
        if not isinstance(row, Track):
            self.notify("Select a track to edit", timeout=3)
            return
        if row.source != SOURCE_LOCAL:
            self.notify("Metadata editing is for local tracks only", severity="warning", timeout=4)
            return

        def handle(result: Any) -> None:
            if isinstance(result, dict):
                self.apply_metadata_edit(row, result)

        self.push_screen(EditMetadataScreen(row), handle)

    @work(thread=True, group="editmeta", exclusive=True)
    def apply_metadata_edit(self, track: Track, data: dict[str, str]) -> None:
        from pathlib import Path as _Path
        from .metadata import CanonicalMetadata, generate_canonical_path, sanitize_component, write_metadata

        src = _Path(track.stream_ref)
        if not src.exists():
            self.call_from_thread(self.notify, f"File not found: {src}", severity="error", timeout=6)
            return
        # Build new metadata
        try:
            tn = int(data.get("track_number", "1").split("/")[0]) if data.get("track_number") else 1
        except ValueError:
            tn = 1
        try:
            dn = int(data.get("disc_number", "1").split("/")[0]) if data.get("disc_number") else 1
        except ValueError:
            dn = 1
        meta = CanonicalMetadata(
            title=data.get("title", track.title) or track.title,
            artist=data.get("artist", track.artist) or track.artist,
            album_artist=data.get("album_artist", getattr(track, "album_artist", "") or track.artist) or track.artist,
            album=data.get("album", track.album) or track.album,
            track_number=tn,
            disc_number=dn,
            year=data.get("year", str(track.year or "")) if data.get("year") is not None else str(track.year or ""),
            genre=data.get("genre", getattr(track, "genre", "") or "") or "",
            source_path=str(src),
        )
        # Write tags
        if not write_metadata(src, meta):
            self.call_from_thread(self.notify, "Could not write tags (unsupported format?)", severity="warning", timeout=6)
        # Recalculate canonical path under music root
        root = _Path(self.config.resolved_music_folder())
        if not root.is_dir():
            self.call_from_thread(self.notify, "Music folder not configured", severity="error", timeout=6)
            return
        dest = generate_canonical_path(root, meta)
        # Use find_available_path logic with duplicate handling
        from .metadata import find_available_path

        actual = find_available_path(dest) if dest != src else dest
        if actual != src:
            try:
                actual.parent.mkdir(parents=True, exist_ok=True)
                # safe move via metadata pipeline logic (copy+verify not needed for single)
                src.rename(actual)
                self.call_from_thread(self.notify, f"Moved to {actual.relative_to(root)}", timeout=5)
            except Exception as e:
                self.call_from_thread(self.notify, f"Move failed: {e}", severity="error", timeout=6)
                return
        else:
            self.call_from_thread(self.notify, "Metadata updated", timeout=4)
        # Invalidate cache and reload
        self.local_snapshot = None
        self.call_from_thread(self.reload_current)

    # ---------------------------------------------------------------- source

    def action_switch_source(self) -> None:
        if self.active_source == SOURCE_JELLYFIN:
            folder = self.config.resolved_music_folder()
            if not folder or not Path(folder).is_dir():
                self.notify(
                    "No local music folder yet — set it in ⚙ Server / Setup "
                    "(both sources can be saved at once)",
                    severity="warning",
                    timeout=8,
                )
                self.action_open_setup()
                return
            self.local_lib = None
            self.local_snapshot = None
            self._persist(active_source=SOURCE_LOCAL)
            self.stack = [(LEVEL_ALBUMS, {})]
            self.reload_current()
            self.notify("Source: local library", timeout=3)
            return

        if self.client is not None:
            self._persist(active_source=SOURCE_JELLYFIN)
            self.stack = [(LEVEL_ALBUMS, {})]
            self.reload_current()
            self.notify("Source: Jellyfin server", timeout=3)
            return

        url = self.config.resolved_server()
        user = self.config.resolved_username()
        password = cfg.get_password()
        if url and user and password:
            self.notify(f"Connecting to {url}…", timeout=4)
            self.try_connect(url, user, password)
            return
        self.notify(
            "Fill the server details once in ⚙ Server / Setup to enable streaming",
            severity="warning",
            timeout=6,
        )
        self.action_open_setup()

    def action_rescan(self) -> None:
        if self.active_source == SOURCE_LOCAL:
            self.local_lib = None
            self.local_snapshot = None
        elif self.client is not None:
            self.client.album_cache = None
        self.reload_current()
        self.notify("Rescanning…", timeout=2)

    # ------------------------------------------------------------------ misc

    def action_go_back(self) -> None:
        box = self.query_one("#searchbox", Input)
        if box.has_class("visible") and box.has_focus:
            box.remove_class("visible")
            self.query_one("#table", LibraryTable).focus()
            return
        if len(self.stack) > 1:
            self.stack.pop()
            self.reload_current()

    def action_refresh(self) -> None:
        self.reload_current()

    def action_help_keys(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        if self.player is not None:
            self.player.shutdown()


def run(
    force_setup: bool = False,
    local_root: str | None = None,
    server_override: str | None = None,
) -> None:
    cfg.ensure_cache_dirs()
    ListenerApp(
        force_setup=force_setup,
        local_root=local_root,
        server_override=server_override,
    ).run()
