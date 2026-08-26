"""Configuration and secret storage for Jellyfin Music Listener CLI.

Non-secret settings live in a per-user JSON file under the XDG config
directory. The Jellyfin password is stored in the operating system keyring
when available, with a user-only dotenv fallback for headless sessions.
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
        return _environment_value("JELLYFIN_URL", self.server_url).strip()

    def resolved_username(self) -> str:
        return _environment_value("JELLYFIN_USERNAME", self.username).strip()

    def resolved_music_folder(self) -> str:
        folder = _environment_value("MUSIC_FOLDER", self.music_folder).strip()
        if not folder:
            return ""
        return str(Path(folder).expanduser())


def _env_password() -> str:
    return _environment_value("JELLYFIN_PASSWORD", "").strip()


def dotenv_path() -> Path:
    return CONFIG_PATH.with_name(".env")


def _dotenv_values() -> dict[str, str]:
    try:
        lines = dotenv_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[key] = value
    return values


def _environment_value(name: str, default: str) -> str:
    if name in os.environ:
        return os.environ[name]
    return _dotenv_values().get(name, default)


def _dotenv_key(line: str) -> str:
    line = line.strip()
    if line.startswith("export "):
        line = line[7:].lstrip()
    return line.partition("=")[0].strip()


def _save_dotenv_password(password: str) -> bool:
    path = dotenv_path()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.chmod(stat.S_IRWXU)
        entry = f"JELLYFIN_PASSWORD={json.dumps(password)}\n"
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            lines = []
        replaced = False
        updated = []
        for line in lines:
            if _dotenv_key(line) == "JELLYFIN_PASSWORD":
                if not replaced:
                    updated.append(entry)
                    replaced = True
                continue
            updated.append(line)
        if not replaced:
            updated.append(entry)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(updated), encoding="utf-8")
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(path)
        return True
    except OSError:
        return False


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


def save_password(password: str) -> str:
    password = password.strip()
    if not password:
        return ""
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, password)
        return "keyring"
    except Exception:
        return "dotenv" if _save_dotenv_password(password) else ""


def clear_password() -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception:
        pass
    path = dotenv_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if _dotenv_key(line) != "JELLYFIN_PASSWORD"]
        if kept:
            path.write_text("".join(kept), encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        else:
            path.unlink()
    except OSError:
        pass


def ensure_cache_dirs() -> None:
    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
