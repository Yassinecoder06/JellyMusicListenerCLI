# Jellyfin Music Listener CLI (`jmlcli`)

A keyboard-first terminal music listener for Jellyfin. Browse artists, albums, tracks and playlists from your Jellyfin server — or a local `Artist/Album (Year)/` folder — with album covers drawn at their **true resolution** inside the terminal, full playback control, and instant playlist management from the TUI or the shell.

```
♪ Jellyfin Music Listener CLI  │  [SERVER] https://jellyfin.example.me · you
┌ sidebar ┐ ┌──────────────── main list ────────────────┐
│ 🔎 Search │ │ #  Title          Artist     Album   Time │
│ 🎵 Tracks │ │ 1  Nightcall     Neon      Drive   4:32 │
│ 💿 Albums │ │ 2  City Lights   Neon      Drive   3:58 │
│ 🎤 Artists│ └───────────────────────────────────────────┘
│ 🎧 Lists  │  ▓▓████▓▓   Nightcall · Neon · Drive (2021)
│ ↔ source  │  ▶ 1:24 ━━━━━━━●────────── 4:32   vol 70% ⟳all
└──────────┘
```

## Highlights

- **Two first-class sources**: your Jellyfin server (streaming, playlist sync, now-playing reporting) and local folders organized the Jellyfin way. Press **F2** to flip between them instantly.
- **True-resolution covers**: artwork is sent through the terminal's native image protocol — Kitty graphics protocol on ghostty/kitty/WezTerm, Sixel elsewhere — scaled pixel-perfect to fit its frame. Basic terminals fall back to half-block rendering automatically.
- **Playlists anywhere**: build and edit server playlists from the TUI (`a`/`n`/`d`) or straight from the shell (`jmlcli playlist add "Road Trip" "city lights"`).
- **Scriptable CLI**: search, play, browse and manage everything without opening the TUI.

## Requirements

- Python 3.10+
- The `libmpv` system library for playback (`sudo apt install mpv` provides it; most desktops already have `libmpv.so.2`)
- A reachable Jellyfin 10.x server *and/or* a locally organized music folder

## Install

```bash
cd JellyfinMusicListenerCLI
./install.sh
```

The installer creates or repairs `.venv`, installs every Python dependency, and registers `jmlcli` under `~/.local/bin`. It does not use `sudo`.

Open a new terminal if needed, then run:

```bash
jmlcli
```

`~/.local/bin` must be on your `PATH`. If the command is not found in the current shell, run:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Manual installation remains available for development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
ln -sfn "$PWD/.venv/bin/jmlcli" ~/.local/bin/jmlcli
```

## Usage

### Interactive player

```bash
jmlcli                # opens the TUI (same as: jmlcli tui)
jmlcli tui --local ~/music
jmlcli tui --setup    # jump straight to setup
```

First run shows the setup screen: fill the Jellyfin URL + username + password, and/or a local music folder. Both can be saved at once; F2 switches sources anytime. The password is stored in the OS keyring, never in files.

| Key | Action |
| --- | --- |
| enter | play track / open album, artist or playlist |
| space | pause / resume |
| v | stop |
| z / x | previous / next |
| ← / → | seek ±10 s |
| + / − | volume · m mute |
| s | shuffle · r repeat off→all→one |
| / | search |
| n | new playlist |
| a | add highlighted track or whole album to playlist |
| d | remove entry from current playlist |
| D | delete playlist (on Playlists list) |
| f2 | switch source server ⇄ local |
| f5 | refresh view |
| esc | back · ? help overlay · q quit |

### Scriptable commands

```bash
jmlcli status                                   # config summary + live connection check
jmlcli source local                             # switch default source (server|local)

jmlcli setup --url https://jellyfin.example.me \
             --username you --password s3cret   # one-time; password goes to keyring
jmlcli setup --folder ~/music                   # add/replace local library

jmlcli search "midnight city"                   # table of matches
jmlcli play "the nights avicii"                 # stream best match; Ctrl+C stops
jmlcli play "drive" --index 2 --local           # pick another result, local library

jmlcli library albums --limit 50                # artists | albums | tracks
jmlcli library tracks --local

jmlcli playlist list
jmlcli playlist create "Night Drive"
jmlcli playlist add "Night Drive" "neon nights"          # adds best match
jmlcli playlist add "Night Drive" "kavinsky" --all       # adds every match
jmlcli playlist show "Night Drive"
jmlcli playlist remove-from "Night Drive" "nightcall"
jmlcli playlist delete "Night Drive"

jmlcli --version
```

Exit codes: `0` success, `1` usage/resolution errors, `2` setup saved but login could not be verified.

### Environment overrides

See `.env.example`: `JELLYFIN_URL`, `JELLYFIN_USERNAME`, `JELLYFIN_PASSWORD`, `MUSIC_FOLDER` beat saved settings for headless/CI sessions.

## Local folder layout

Matches the Jellyfin organizer convention:

```text
MUSIC_FOLDER/
└── Artist/
    └── Album (Year)/
        ├── 01 - Title.mp3
        ├── 07 - Another.flac
        └── cover.jpg
```

Audio: mp3, m4a, aac, flac, ogg, opus, wav, wma. Tags via mutagen when present, otherwise filenames are parsed (`01 - Title`). Covers: `cover.*`, `folder.*`, `front.*`, `album.*`, `art.*`, or any image in the album folder.

## How it works

- **Streaming**: direct download URLs authenticated with the session token (`/Items/{id}/Download?api_key=…`), decoded by libmpv. Progress is reported to `/Sessions/Playing*` so the Jellyfin dashboard reflects your listening.
- **Covers**: fetched at original resolution and cached under `~/.cache/jellyfin-music-listener/covers/`; rendered through the Kitty graphics protocol or Sixel when the terminal supports them, otherwise as half-block ANSI.
- **Config**: non-secret settings in `~/.config/jellyfin-music-listener/config.json` (user-only permissions); the password lives in the system keyring.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Covers config storage, models, cover fitting/rendering, the local scanner, the Jellyfin client against a mock server, libmpv playback (play/pause/seek/EOF), headless TUI flows, and the `jmlcli` commands.

## Project layout

```text
install.sh                  one-command, no-sudo installer
pyproject.toml              packaging; installs the jmlcli script
music_listener/
├── cli.py                  jmlcli argparse front door + subcommands
├── app.py                  Textual UI: navigation, queue, modals, source switching
├── jellyfin.py             Jellyfin REST client
├── locallib.py             local library scanner
├── coverart.py             fallback half-block renderer + disk cache
├── player.py               libmpv engine wrapper
├── config.py               settings file + keyring secrets
├── models.py               shared Track/Album/Artist/Playlist types
└── widgets/
    ├── cover.py            true-resolution (TGP/Sixel) + fallback covers
    └── nowplaying.py       bottom now-playing bar
tests/                      unit, integration and pilot-driven TUI tests
```

Design inspiration: cliamp's provider model and Winamp-style keyboard flow; AudioMatic's focused dark listening experience.
