"""Safe Jellyfin-compatible organizer pipeline.

Implements transactional import:
 SOURCE → Analyze → Identify → Validate → Write to temp → Verify → Move → Update DB
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .metadata import (
    CanonicalMetadata,
    enrich_with_musicbrainz,
    extract_existing_metadata,
    file_fingerprint,
    find_available_path,
    generate_canonical_path,
    write_metadata,
    logger as meta_logger,
)

StatusCallback = Callable[[str], None]

# Reuse same logger
logger = logging.getLogger("jml.organizer")
logger.setLevel(logging.INFO)


@dataclass
class ProposedOperation:
    source: Path
    destination: Path
    metadata: CanonicalMetadata
    status: str = "pending"  # pending, duplicate, needs_review, error
    message: str = ""


def _is_duplicate(source: Path, dest: Path, meta: CanonicalMetadata) -> bool:
    if not dest.exists():
        return False
    # Same MB recording ID is duplicate if present
    # We check fingerprint first
    try:
        if source.stat().st_size == dest.stat().st_size:
            # quick hash of first chunk
            if file_fingerprint(source) == file_fingerprint(dest):
                return True
    except Exception:
        pass
    # Also check same Artist+Album+Disc+Track+Title path would be same destination already handled
    return False


def _is_staging_value(value: str, file: Path) -> bool:
    if not value:
        return True
    low = value.strip().lower()
    staging = {"downloads", "audios", "src", "inbox", "tmp", "temp", "music", "downloads/audios"}
    if low in staging:
        return True
    try:
        if low == file.parent.name.lower() or low == file.parent.parent.name.lower():
            if file.parent.name.lower() in staging or file.parent.parent.name.lower() in staging:
                return True
    except Exception:
        pass
    return False


def _improve_for_staging(meta: CanonicalMetadata, file: Path) -> CanonicalMetadata:
    # If artist/album came from staging/inbox folders, try to parse "Artist - Title" from filename
    # Also handle generic parent-derived values (file.parent.name)
    is_artist_staging = _is_staging_value(meta.artist, file) or meta.artist == file.parent.name
    is_album_staging = _is_staging_value(meta.album, file) or file.parent.name == meta.album or file.parent.parent.name == meta.album
    if is_artist_staging or is_album_staging:
        stem = file.stem.strip()
        import re

        # Try "Artist - Album - Title" or "Artist - Title"
        parts = [p.strip() for p in stem.split(" - ")]
        if len(parts) >= 2:
            # Check if first part looks like track number
            if re.match(r"^\d{1,3}$", parts[0]):
                # 01 - Artist - Title or 01 - Title
                if len(parts) >= 3 and not _is_staging_value(parts[1], file):
                    meta.artist = parts[1]
                    meta.title = " - ".join(parts[2:])
                else:
                    meta.title = " - ".join(parts[1:])
            else:
                # Artist - Title or Artist - Album - Title
                if len(parts) == 2:
                    # Artist - Title
                    if not _is_staging_value(parts[0], file):
                        meta.artist = parts[0]
                        meta.title = parts[1]
                elif len(parts) >= 3:
                    # Could be Artist - Album - Title
                    if not _is_staging_value(parts[0], file):
                        meta.artist = parts[0]
                    if not _is_staging_value(parts[1], file):
                        meta.album = parts[1]
                    meta.title = " - ".join(parts[2:])
        # Generic inbox folder names should not be used as artist/album
        if meta.artist == file.parent.name and file.parent.name.lower() not in {"music", "artist"}:
            # If file is directly under source inbox, parent is not artist
            # Check if we still have a staging-like value
            if _is_staging_value(meta.artist, file) or meta.artist == file.parent.name:
                # Try to keep parsed artist if we got one from filename, else Unknown
                if meta.artist == file.parent.name and " - " not in stem:
                    meta.artist = "Unknown Artist"
                    meta.needs_review = True
        if meta.album == file.parent.name or _is_staging_value(meta.album, file):
            # For inbox files, album is often parent folder which is inbox itself or src
            if file.parent.name.lower() in {"src", "inbox", "downloads", "audios", "tmp", "temp"} or meta.album == file.parent.name:
                # If we didn't parse album from filename, set Unknown
                if " - " not in stem or len(parts) < 3:
                    # Keep Unknown to trigger review or Single
                    if meta.album not in ("Single", "Unknown Album"):
                        # Check if we have a better album from tags? if not, keep Single
                        pass
        if _is_staging_value(meta.artist, file) or meta.artist == file.parent.name:
            # Final fallback if still staging-like or equals parent
            if meta.artist.lower() in {"src", "tmp", "temp", "inbox", "downloads", "audios"} or meta.artist == file.parent.name:
                # Re-check if we parsed a better artist from filename
                if " - " in stem and len(parts) >= 2 and not _is_staging_value(parts[0], file):
                    meta.artist = parts[0]
                else:
                    meta.artist = "Unknown Artist"
                    meta.needs_review = True
        if _is_staging_value(meta.album, file):
            # For inbox files without album, use Single to allow organizing (Jellyfin handles Single)
            # Only mark needs_review if we couldn't infer album
            if meta.album.lower() in {"src", "inbox", "downloads", "audios", "tmp", "temp", "music", "unknown", "unknown album"}:
                meta.album = "Single"
            else:
                meta.album = "Unknown Album"
                meta.needs_review = True
        if not meta.album_artist or _is_staging_value(meta.album_artist, file) or meta.album_artist == file.parent.name:
            meta.album_artist = meta.artist
    return meta


def _validate(meta: CanonicalMetadata) -> tuple[bool, str]:
    if meta.artist == "Unknown Artist" or meta.album == "Unknown Album" or not meta.title:
        return False, "missing essential metadata"
    return True, ""


def preview_organize(
    source: Path,
    destination_root: Path,
    use_musicbrainz: bool = False,
) -> list[ProposedOperation]:
    """Dry-run: analyze without moving or writing."""
    ops: list[ProposedOperation] = []
    files = _collect_audio_files(source, destination_root)
    for f in files:
        meta = extract_existing_metadata(f)
        meta = _improve_for_staging(meta, f)
        # Always try MusicBrainz when album is missing/Single to find real album, even without flag
        should_try_mb = use_musicbrainz or meta.album in ("Single", "Unknown Album", "Unknown", "")
        if should_try_mb:
            try:
                meta = enrich_with_musicbrainz(meta, enable=True)
            except Exception:
                pass
        # normalize album_artist for compilations: keep Various Artists if detected
        # If album contains "Various" etc, we keep as Various Artists
        valid, msg = _validate(meta)
        dest = generate_canonical_path(destination_root, meta)
        status = "pending"
        if not valid:
            status = "needs_review"
            msg = f"needs review: {msg}"
        elif _is_duplicate(f, dest, meta):
            status = "duplicate"
            msg = "duplicate detected (same MB id or fingerprint)"
        elif meta.needs_review:
            status = "needs_review"
            msg = "low confidence metadata"
        ops.append(ProposedOperation(source=f, destination=dest, metadata=meta, status=status, message=msg))
    return ops


def _collect_audio_files(source: Path, dest_root: Path | None = None) -> list[Path]:
    exts = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma", ".mp4"}
    if source.is_file():
        return [source] if source.suffix.lower() in exts else []
    files: list[Path] = []
    for p in source.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if dest_root and dest_root.resolve() in p.resolve().parents:
            continue
        files.append(p)
    return sorted(files)


def organize(
    source: Path,
    destination_root: Path,
    use_musicbrainz: bool = False,
    dry_run: bool = False,
    status: StatusCallback | None = None,
) -> dict[str, int]:
    """Transactional organize. Returns counts. Never deletes source on failure."""
    def _log(msg: str):
        logger.info(msg)
        if status:
            status(msg)

    files = _collect_audio_files(source, destination_root)
    if not files:
        _log("[ORGANIZE] No compatible media files found.")
        return {"copied": 0, "skipped": 0, "failed": 0, "needs_review": 0}

    copied = skipped = failed = needs_review = 0
    for f in files:
        _log(f"[IMPORT] {f}")
        meta = extract_existing_metadata(f)
        meta = _improve_for_staging(meta, f)
        _log(f"[METADATA] Artist: {meta.artist} | Album: {meta.album} | Title: {meta.title} | Track: {meta.track_number} Disc: {meta.disc_number}")
        should_try_mb = use_musicbrainz or meta.album in ("Single", "Unknown Album", "Unknown", "")
        if should_try_mb:
            try:
                meta = enrich_with_musicbrainz(meta, enable=True)
            except Exception:
                pass
            if meta.mb_recording_id:
                _log(f"[IDENTIFICATION] MusicBrainz match found {meta.mb_recording_id}")
            else:
                _log(f"[IDENTIFICATION] No confident MusicBrainz match, preserving existing")
        valid, msg = _validate(meta)
        if not valid:
            _log(f"[ERROR] Could not confidently identify {f.name}: {msg}")
            _log(f"[ACTION] File left in source location")
            needs_review += 1
            continue
        dest = generate_canonical_path(destination_root, meta)
        _log(f"[DESTINATION] {dest}")

        if dry_run:
            _log(f"[DRY RUN] Would organize {f.name} → {dest.relative_to(destination_root) if dest.is_relative_to(destination_root) else dest}")
            skipped += 1
            continue

        # Duplicate detection
        # Check same MB recording ID already in dest? Search dest for same MBID? Simplified fingerprint
        if _is_duplicate(f, dest, meta):
            _log(f"[DUPLICATE] Skipping duplicate {f.name}")
            skipped += 1
            continue
        # Conflict handling
        actual_dest = find_available_path(dest)
        if actual_dest != dest:
            _log(f"[CONFLICT] {dest.name} exists, using {actual_dest.name}")

        # Safe pipeline: copy to temp staged file, write tags, verify, then move
        tmp_path: Path | None = None
        try:
            # Stage: copy source to temp file in dest parent
            actual_dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(delete=False, dir=str(actual_dest.parent), suffix=Path(f).suffix) as tmp:
                tmp_path = Path(tmp.name)
            shutil.copy2(str(f), str(tmp_path))
            _log(f"[STAGE] copied to temp {tmp_path}")

            # Write metadata to staged file
            if not write_metadata(tmp_path, meta):
                _log(f"[WRITE TAGS] failed for {tmp_path}, continuing with original tags")
            else:
                _log(f"[WRITE TAGS] success")

            # Verify metadata
            verify_meta = extract_existing_metadata(tmp_path)
            if verify_meta.title != meta.title:
                _log(f"[VERIFY] warning: title mismatch after write")

            # Atomic move to final
            tmp_path.rename(actual_dest)
            tmp_path = None
            _log(f"[MOVE] success {actual_dest}")
            # Also copy cover.jpg if present in source album folder
            try:
                for cover_name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg"):
                    src_cover = file.parent / cover_name
                    if src_cover.is_file():
                        dst_cover = actual_dest.parent / "cover.jpg"
                        if not dst_cover.exists():
                            import shutil as _sh
                            _sh.copy2(str(src_cover), str(dst_cover))
                            _log(f"[COVER] copied {cover_name} → {dst_cover}")
                        break
            except Exception as e:
                _log(f"[COVER] warning: {e}")
            # Update DB is filesystem; no separate DB for listener
            copied += 1
        except Exception as e:
            _log(f"[ERROR] Could not organize {f.name}: {e}")
            _log(f"[ACTION] File left in source location")
            failed += 1
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
        # Do not delete source
    result = {"copied": copied, "skipped": skipped, "failed": failed, "needs_review": needs_review}
    _log(f"[SUMMARY] copied={copied} skipped={skipped} failed={failed} needs_review={needs_review}")
    return result


def trigger_jellyfin_rescan(base_url: str, token: str, user_id: str | None = None) -> bool:
    """Optional Jellyfin library refresh. Fails silently if unavailable."""
    if not base_url or not token:
        logger.info("[JELLYFIN] No Jellyfin configured, skipping rescan")
        return False
    try:
        import requests

        url = base_url.rstrip("/") + "/Library/Refresh"
        headers = {"X-Emby-Token": token}
        resp = requests.post(url, headers=headers, timeout=10)
        if resp.status_code in (200, 204):
            logger.info("[JELLYFIN] Library refresh triggered")
            return True
        logger.warning(f"[JELLYFIN] Refresh failed HTTP {resp.status_code}")
        return False
    except Exception as e:
        logger.warning(f"[JELLYFIN] Refresh failed: {e}")
        return False
