"""Footage-only cache lifecycle. Transcript caches and final outputs are never pruned."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

HEX = re.compile(r"[0-9a-f]{64}")
DEFAULT_MAX_BYTES = 4 * 1024**3
DEFAULT_MAX_AGE = 30 * 86400


def _managed(root):
    """Recognize current and legacy files; never follow user-created symlinks."""
    if root.is_symlink() or not root.exists():
        return
    for directory in root.iterdir():
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not HEX.fullmatch(directory.name)
        ):
            continue
        owned = any(
            (directory / name).is_file() and not (directory / name).is_symlink()
            for name in ("source.json", "metadata.json")
        )
        for p in directory.rglob("*"):
            if p.is_symlink() or any(
                a.is_symlink() for a in p.parents if a == root or root in a.parents
            ):
                continue
            if not p.is_file():
                continue
            temporary = bool(
                re.fullmatch(r"tmp[a-z0-9_]{8}\.(part|mp4)", p.name)
            ) or bool(re.fullmatch(r"frames-[a-z0-9_]{8}", p.parent.name))
            reusable = (
                p.name
                in {
                    "source.media",
                    "source.json",
                    "metadata.json",
                    "cues.json",
                    "index.json",
                    "access",
                }
                or HEX.fullmatch(p.stem)
                and p.suffix in {".json", ".mp4"}
                or p.parent.name == "index"
                and re.fullmatch(r"\d{6}\.jpg", p.name)
            )
            if temporary or owned and reusable:
                yield directory, p, temporary


def status(root: Path) -> dict:
    result = {
        "root": str(root),
        "bytes": 0,
        "files": 0,
        "temporary_bytes": 0,
        "temporary_files": 0,
    }
    for _, p, temporary in _managed(root):
        try:
            result["bytes"] += p.stat().st_size
            result["files"] += 1
            if temporary:
                result["temporary_bytes"] += p.stat().st_size
                result["temporary_files"] += 1
        except FileNotFoundError:
            pass
    return result


@contextmanager
def guard(root, name=".maintenance"):
    """Portable cross-process exclusion, with recovery of dead owners."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    started = time.monotonic()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({"pid": os.getpid()}, out)
            break
        except FileExistsError:
            try:
                pid = int(json.loads(path.read_text())["pid"])
                if pid > 0:
                    os.kill(pid, 0)
            except ProcessLookupError:
                path.unlink(missing_ok=True)
                continue
            except (OSError, ValueError, KeyError):
                pass
            if time.monotonic() - started > 900:
                raise TimeoutError("cache busy")
            time.sleep(0.05)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _active(root, clean_stale=True):
    for p in (root / ".leases").glob("*.json"):
        try:
            pid = int(json.loads(p.read_text())["pid"])
            if pid <= 0:
                return True
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            if clean_stale:
                p.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            return True  # unknown owner: conservative, never prune a live run
    return False


@contextmanager
def lease(root: Path):
    directory = root / ".leases"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid.uuid4().hex + ".json")
    with guard(root):
        path.write_text(json.dumps({"pid": os.getpid()}))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def touch(directory):
    (directory / "access").touch()


def _prune(
    root: Path,
    max_bytes=DEFAULT_MAX_BYTES,
    max_age=DEFAULT_MAX_AGE,
    clean=False,
    dry_run=False,
    now=None,
    temporary_age=86400,
) -> dict:
    if max_bytes < 0 or max_age < 0 or temporary_age < 0:
        raise ValueError("cache limits must be nonnegative")
    now = time.time() if now is None else now
    if _active(root, clean_stale=not dry_run):
        return {"removed_files": 0, "removed_bytes": 0, "busy": True}
    groups = {}
    for directory, p, temporary in _managed(root):
        try:
            stat = p.stat()
            entry = groups.setdefault(directory, {"files": [], "bytes": 0, "used": 0})
            entry["files"].append((p, stat.st_size, temporary, stat.st_mtime))
            entry["bytes"] += stat.st_size
            entry["used"] = max(entry["used"], stat.st_mtime)
        except FileNotFoundError:
            continue
    total = sum(e["bytes"] for e in groups.values())
    removed_files = removed_bytes = 0
    for directory, entry in sorted(groups.items(), key=lambda item: item[1]["used"]):
        evict = clean or now - entry["used"] > max_age or total > max_bytes
        for p, size, temporary, modified in entry["files"]:
            if evict or (temporary and now - modified > temporary_age):
                if not dry_run:
                    p.unlink(missing_ok=True)
                removed_files += 1
                removed_bytes += size
                total -= size
        if not dry_run:
            for p in sorted(
                directory.rglob("*"), key=lambda p: len(p.parts), reverse=True
            ):
                if p.is_dir() and not p.is_symlink():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "busy": False,
        "remaining_bytes": total,
        "dry_run": dry_run,
    }


def prune(root: Path, **kwargs):
    if kwargs.get("dry_run"):
        return _prune(root, **kwargs)
    with guard(root):
        return _prune(root, **kwargs)
