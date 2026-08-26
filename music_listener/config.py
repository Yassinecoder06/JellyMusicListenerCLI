"""Configuration and secret storage for Jellyfin Music Listener CLI.

Non-secret settings live in a per-user JSON file under the XDG config
directory.  The Jellyfin password is stored in the operating system
keyring when available; environment variables override everything for
headless sessions.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

APP_NAME = "jellyfin-music-listener"
CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
)
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / APP_NAME
COVER_CACHE_DIR = CACHE_DIR / "covers"
KEYRING_SERVICE = APP_NAME
KEYRING_ACCOUNT = "jellyfin-password"

REPEAT_OFF = "off"
REPEAT_ALL = "all"
REPEAT_ONE = "one"


@dataclass
class AppConfig:
    server_url: str = ""
    username: str = ""
    music_folder: str = ""
    active_source: str = "jellyfin"
    volume: float = 100.0
    shuffle: bool = False
    repeat: str = REPEAT_OFF
    device_id: str = ""

    def resolved_server(self) -> str:
        return os.environ.get("JELLYFIN_URL", self.server_url).strip()

    def resolved_username(self) -> str:
        return os.environ.get("JELLYFIN_USERNAME", self.username).strip()

    def resolved_music_folder(self) -> str:
        folder = os.environ.get("MUSIC_FOLDER", self.music_folder).strip()
        if not folder:
            return ""
        return str(Path(folder).expanduser())


def _env_password() -> str:
    return os.environ.get("JELLYFIN_PASSWORD", "").strip()


def _read_saved() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return saved if isinstance(saved, dict) else {}


def load_config() -> AppConfig:
    values = asdict(AppConfig())
    values.update(
        {k: v for k, v in _read_saved().items() if k in values}
    )
    config = AppConfig(**values)
    if not config.device_id:
        config.device_id = uuid.uuid4().hex[:16]
        config = save_config(config)
    return config


def save_config(config: AppConfig) -> AppConfig:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(stat.S_IRWXU)
    except OSError:
        pass
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(CONFIG_PATH)
    return config


def update_config(**changes: Any) -> AppConfig:
    config = load_config()
    known = {f.name for f in fields(AppConfig)}
    for key, value in changes.items():
        if key in known:
            setattr(config, key, value)
    return save_config(config)


def get_password() -> str:
    env = _env_password()
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or ""
    except Exception:
        return ""


def save_password(password: str) -> bool:
    password = password.strip()
    if not password:
        return False
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, password)
        return True
    except Exception:
        return False


def clear_password() -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception:
        pass


def ensure_cache_dirs() -> None:
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
