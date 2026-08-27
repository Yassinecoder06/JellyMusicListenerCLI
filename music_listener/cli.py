"""Command-line interface for the Jellyfin Music Listener CLI.

`jmlcli` with no arguments opens the interactive TUI. Subcommands expose
library browsing, searching, playback and full playlist management for
scripts and quick terminal use.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

APP_NAME = "jellyfin-music-listener"
APP_VERSION = "1.1.2"


# ---------------------------------------------------------------- helpers


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _ok(message: str) -> None:
    print(message)


def _load_config():
    from . import config as cfg

    return cfg.load_config()


def _connect_with_stored(config):
    from . import config as cfg
    from .jellyfin import JellyfinError, connect

    url = config.resolved_server()
    user = config.resolved_username()
    password = cfg.get_password()
    if not url or not user:
        raise RuntimeError(
            "No Jellyfin server configured. Run: jmlcli setup --url URL --username USER"
        )
    return connect(url, user, password, config.device_id)


def _resolve_query_tracks(client, query: str, limit: int = 50):
    return client.search_tracks(query, limit)


def _local_snapshot(config):
    from .locallib import LocalLibrary

    folder = config.resolved_music_folder()
    if not folder:
        raise RuntimeError("No local music folder configured. Run: jmlcli setup --folder PATH")
    return LocalLibrary(folder).scan()


def _local_filter(snapshot, query: str, limit: int = 50):
    q = query.lower()
    return [
        t
        for t in snapshot.tracks
        if q in t.title.lower() or q in t.artist.lower() or q in t.album.lower()
    ][:limit]


def _print_tracks(tracks) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title")
    table.add_column("Artist", style="cyan")
    table.add_column("Album", style="magenta")
    table.add_column("Time", justify="right")
    for i, t in enumerate(tracks, start=1):
        table.add_row(str(i), t.title, t.artist, t.album, t.duration_text)
    console = Console()
    if tracks:
        console.print(table)
    else:
        console.print("[dim]No results.[/dim]")


def _find_playlist(client, name_or_id: str):
    from rich.console import Console

    playlists = client.playlists()
    lowered = name_or_id.lower()
    for pl in playlists:
        if pl.id == name_or_id or pl.id.startswith(name_or_id):
            return pl
    for pl in playlists:
        if pl.name.lower() == lowered:
            return pl
    partial = [pl for pl in playlists if lowered in pl.name.lower()]
    if len(partial) == 1:
        return partial[0]
    console = Console()
    if partial:
        console.print(f"[yellow]Ambiguous playlist name '{name_or_id}':[/yellow]")
        for pl in partial:
            console.print(f"  - {pl.name} ({pl.track_count} tracks)")
    else:
        console.print(f"[red]Playlist '{name_or_id}' not found.[/red]")
        for pl in playlists:
            console.print(f"  - {pl.name} ({pl.track_count} tracks)")
    raise RuntimeError(f"playlist '{name_or_id}' not resolved")


# --------------------------------------------------------------- commands


def cmd_tui(args) -> int:
    from .app import run

    run(force_setup=args.setup, local_root=args.local, server_override=args.server)
    return 0


def cmd_setup(args) -> int:
    from . import config as cfg
    from .models import SOURCE_JELLYFIN, SOURCE_LOCAL

    provided_any = any(
        getattr(args, name) is not None
        for name in ("url", "username", "password", "folder", "source")
    )
    if not provided_any:
        from .app import run

        run(force_setup=True)
        return 0

    updates: dict[str, Any] = {}
    if args.url is not None:
        from .jellyfin import normalize_base_url

        updates["server_url"] = normalize_base_url(args.url)
    if args.username is not None:
        updates["username"] = args.username
    if args.folder is not None:
        expanded = str(Path(args.folder).expanduser())
        if not Path(expanded).is_dir():
            return _fail(f"Not a folder: {expanded}")
        updates["music_folder"] = expanded
    if args.source == "server":
        updates["active_source"] = SOURCE_JELLYFIN
    elif args.source == "local":
        updates["active_source"] = SOURCE_LOCAL

    auth_error = None
    url = updates.get("server_url", cfg.load_config().resolved_server())
    user = updates.get("username", cfg.load_config().resolved_username())
    if url and user and args.password is not None:
        storage = cfg.save_password(args.password)
        if storage == "dotenv":
            _ok(f"password saved to protected fallback file: {cfg.dotenv_path()}")
        elif not storage:
            _ok("note: password could not be persisted")
        try:
            _connect_with_stored_type(url, user, args.password, config_device())
            _ok(f"connected to {url}")
        except Exception as error:
            auth_error = error
            _ok(f"warning: could not verify login: {error}")
    elif args.password is not None:
        storage = cfg.save_password(args.password)
        if storage == "keyring":
            _ok("password stored in the system keyring")
        elif storage == "dotenv":
            _ok(f"password saved to protected fallback file: {cfg.dotenv_path()}")
        else:
            _ok("note: password could not be persisted")

    cfg.update_config(**updates)
    _ok(f"settings saved to {cfg.CONFIG_PATH}")
    return 2 if auth_error else 0


def config_device() -> str:
    return _load_config().device_id


def _connect_with_stored_type(url: str, user: str, password: str, device_id: str):
    from .jellyfin import connect

    return connect(url, user, password, device_id)


def cmd_status(_args) -> int:
    from rich.console import Console
    from rich.table import Table

    from . import config as cfg

    config = _load_config()
    table = Table(show_header=False, box=None)
    table.add_row("config", str(cfg.CONFIG_PATH))
    table.add_row("source", config.active_source)
    table.add_row("server", config.resolved_server() or "[dim]not set[/dim]")
    table.add_row("username", config.resolved_username() or "[dim]not set[/dim]")
    table.add_row("music folder", config.resolved_music_folder() or "[dim]not set[/dim]")
    table.add_row("shuffle", str(config.shuffle))
    table.add_row("repeat", config.repeat)

    if config.resolved_server() and config.resolved_username():
        try:
            client = _connect_with_stored(config)
            table.add_row("connection", f"[green]OK[/green] ({client.username})")
        except Exception as error:
            table.add_row("connection", f"[red]{error}[/red]")
    Console().print(table)
    return 0


def cmd_source(args) -> int:
    from . import config as cfg
    from .models import SOURCE_JELLYFIN, SOURCE_LOCAL

    if not args.which:
        _ok(_load_config().active_source)
        return 0
    which = args.which.lower()
    if which not in (SOURCE_JELLYFIN, SOURCE_LOCAL):
        return _fail("choose 'server' or 'local'")
    cfg.update_config(active_source=which)
    _ok(f"source set to {which}")
    return 0


def cmd_search(args) -> int:
    config = _load_config()
    if args.local:
        tracks = _local_filter(_local_snapshot(config), args.query, args.limit)
    else:
        client = _connect_with_stored(config)
        tracks = _resolve_query_tracks(client, args.query, args.limit)
    _print_tracks(tracks[: args.limit])
    return 0


def cmd_play(args) -> int:
    config = _load_config()
    if args.local:
        tracks = _local_filter(_local_snapshot(config), args.query)
    else:
        client = _connect_with_stored(config)
        tracks = _resolve_query_tracks(client, args.query)
    if not tracks:
        return _fail(f"nothing matched '{args.query}'")
    index = min(max(args.index, 1), len(tracks)) - 1
    track = tracks[index]

    from .player import MpvPlayer

    if track.source == "local":
        ref = track.stream_ref
    else:
        ref = client.stream_url(track.id)

    player = MpvPlayer()
    if status := player.ssh_audio_status:
        print(status, file=sys.stderr)
    player.play(ref)
    print(f"▶ {track.artist} — {track.title}  [{track.album}]")

    def fmt(sec):
        sec = max(int(sec or 0), 0)
        return f"{sec // 60}:{sec % 60:02d}"

    exit_code = 0
    try:
        started = False
        while True:
            status = player.status()
            if status.state == "stopped":
                if started:
                    break
            else:
                started = True
                total = status.duration or track.duration or 0
                pos = status.position or 0
                width = 30
                filled = int(pos / total * width) if total else 0
                bar = "━" * filled + "●" + "─" * max(width - filled - 1, 0)
                print(f"\r{fmt(pos)} {bar} {fmt(total)} ", end="", flush=True)
            time.sleep(0.4)
    except KeyboardInterrupt:
        print()
    finally:
        player.stop()
        player.shutdown()
    return exit_code


def cmd_library(args) -> int:
    config = _load_config()
    if args.local:
        snap = _local_snapshot(config)
        items: list[Any] = {
            "artists": list(snap.artists),
            "albums": list(snap.albums),
            "tracks": list(snap.tracks),
        }[args.what]
    else:
        client = _connect_with_stored(config)
        items = {
            "artists": lambda: client.artists(),
            "albums": lambda: client.albums(),
            "tracks": lambda: client.all_tracks(limit=max(args.limit, 500)),
        }[args.what]()
    if args.what == "tracks":
        _print_tracks(items[: args.limit])
        return 0
    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, header_style="bold")
    table.add_column("Name" if args.what == "artists" else "Album")
    if args.what == "albums":
        table.add_column("Artist", style="cyan")
    table.add_column("Year", justify="right")
    table.add_column("Tracks" if args.what == "albums" else "Albums", justify="right")
    rows = items[: args.limit]
    for item in rows:
        year = "" if item.year is None else str(item.year)
        if args.what == "artists":
            table.add_row(item.name, year, str(item.album_count))
        else:
            table.add_row(item.name, item.artist, year, str(item.track_count))
    Console().print(table)
    return 0


def cmd_playlist(args) -> int:
    action = args.action
    config = _load_config()

    if action == "list":
        client = _connect_with_stored(config)
        from rich.console import Console
        from rich.table import Table

        table = Table(box=None, header_style="bold")
        table.add_column("Playlist")
        table.add_column("Tracks", justify="right")
        for pl in client.playlists():
            table.add_row(pl.name, str(pl.track_count))
        Console().print(table)
        return 0

    client = _connect_with_stored(config)
    if action == "create":
        ids: list[str] = []
        if args.add:
            for t in _resolve_query_tracks(client, args.add)[: 10]:
                ids.append(t.id)
        pid = client.create_playlist(args.name, ids)
        _ok(f"created playlist '{args.name}' ({pid}) with {len(ids)} tracks")
        return 0

    playlist = _find_playlist(client, args.playlist)

    if action == "show":
        _print_tracks(client.playlist_tracks(playlist.id))
        return 0

    if action == "add":
        matches = _resolve_query_tracks(client, args.query)
        if not matches:
            return _fail(f"nothing matched '{args.query}'")
        chosen = matches if args.all else matches[:1]
        client.add_to_playlist(playlist.id, [t.id for t in chosen])
        _ok(f"added {len(chosen)} track(s) to '{playlist.name}'")
        return 0

    if action == "remove-from":
        entries = client.playlist_tracks(playlist.id)
        q = args.query.lower()
        hits = [
            e for e in entries
            if q in e.title.lower() or q in e.artist.lower() or q in e.album.lower()
        ]
        if not hits:
            return _fail(f"'{args.query}' not found in '{playlist.name}'")
        entry_ids = [h.playlist_entry_id for h in hits if h.playlist_entry_id]
        client.remove_from_playlist(playlist.id, entry_ids)
        _ok(f"removed {len(entry_ids)} track(s) from '{playlist.name}'")
        return 0

    if action == "delete":
        client.delete_playlist(playlist.id)
        _ok(f"deleted playlist '{playlist.name}'")
        return 0

    return _fail(f"unknown playlist action: {action}")


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jmlcli",
        description=(
            "Terminal music listener for Jellyfin servers and local "
            "Artist/Album (Year)/ folders. Run without arguments for the "
            "interactive player."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    subs = parser.add_subparsers(dest="command")

    p_tui = subs.add_parser("tui", help="open the interactive player (default)")
    p_tui.add_argument("--setup", action="store_true", help="open setup screen first")
    p_tui.add_argument("--local", metavar="PATH", help="start in local mode")
    p_tui.add_argument("--server", metavar="URL", help="override server URL")
    p_tui.set_defaults(func=cmd_tui)

    p_setup = subs.add_parser("setup", help="configure server and/or local folder")
    p_setup.add_argument("--url", metavar="URL", help="Jellyfin server URL")
    p_setup.add_argument("--username", metavar="USER")
    p_setup.add_argument("--password", metavar="PASS", help="stored in the keyring or protected .env fallback")
    p_setup.add_argument("--folder", metavar="PATH", help="local music folder")
    p_setup.add_argument("--source", choices=["server", "local"], help="set active source")
    p_setup.set_defaults(func=cmd_setup, url=None, username=None, password=None,
                         folder=None, source=None)

    p_status = subs.add_parser("status", help="show configuration and connectivity")
    p_status.set_defaults(func=cmd_status)

    p_src = subs.add_parser("source", help="get or set the active source")
    p_src.add_argument("which", nargs="?", choices=["server", "local"])
    p_src.set_defaults(func=cmd_source)

    p_search = subs.add_parser("search", help="search tracks")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument("--local", action="store_true", help="search the local library")
    p_search.set_defaults(func=cmd_search)

    p_play = subs.add_parser("play", help="search and play the best match (Ctrl+C stops)")
    p_play.add_argument("query")
    p_play.add_argument("--index", type=int, default=1, help="1-based result to play")
    p_play.add_argument("--local", action="store_true", help="search the local library")
    p_play.set_defaults(func=cmd_play)

    p_lib = subs.add_parser("library", help="browse the library")
    p_lib.add_argument("what", choices=["artists", "albums", "tracks"])
    p_lib.add_argument("--limit", type=int, default=100)
    p_lib.add_argument("--local", action="store_true")
    p_lib.set_defaults(func=cmd_library)

    p_pl = subs.add_parser("playlist", help="manage Jellyfin playlists")
    pl_subs = p_pl.add_subparsers(dest="action", required=True)

    p_pl_list = pl_subs.add_parser("list", help="list playlists")
    p_pl_list.set_defaults(func=cmd_playlist)

    p_pl_show = pl_subs.add_parser("show", help="print a playlist's tracks")
    p_pl_show.add_argument("playlist", help="playlist name or id")
    p_pl_show.set_defaults(func=cmd_playlist)

    p_pl_create = pl_subs.add_parser("create", help="create a playlist")
    p_pl_create.add_argument("name")
    p_pl_create.add_argument("--add", metavar="QUERY", help="fill with search matches")
    p_pl_create.set_defaults(func=cmd_playlist)

    p_pl_add = pl_subs.add_parser("add", help="add search matches to a playlist")
    p_pl_add.add_argument("playlist")
    p_pl_add.add_argument("query")
    p_pl_add.add_argument("--all", action="store_true", help="add all matches (default: first)")
    p_pl_add.set_defaults(func=cmd_playlist)

    p_pl_rm = pl_subs.add_parser("remove-from", help="remove matching tracks from a playlist")
    p_pl_rm.add_argument("playlist")
    p_pl_rm.add_argument("query")
    p_pl_rm.set_defaults(func=cmd_playlist)

    p_pl_del = pl_subs.add_parser("delete", help="delete a playlist")
    p_pl_del.add_argument("playlist")
    p_pl_del.set_defaults(func=cmd_playlist)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    func = getattr(args, "func", None)
    if func is None:
        return cmd_tui(argparse.Namespace(setup=False, local=None, server=None))

    try:
        return func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        return _fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
