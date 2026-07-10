"""Debug artifacts for failed or sampled browser fetches."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trawler import config
from trawler.atomic import atomic_write

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]+$")
_TEXT_FILES = {"metadata.json", "page.html", "console.json", "request_failures.json"}
_VALID_MODES = {"off", "fail", "sample", "always"}


def _mode() -> str:
    mode = (getattr(config, "DEBUG_ARTIFACTS", "fail") or "fail").strip().lower()
    return mode if mode in _VALID_MODES else "fail"


def _sample_rate() -> float:
    try:
        rate = float(getattr(config, "ARTIFACT_SAMPLE_RATE", 0.05))
    except (TypeError, ValueError):
        return 0.05
    return max(0.0, min(rate, 1.0))


def should_capture(*, success: bool, url: str = "", reason: str = "") -> bool:
    mode = _mode()
    if mode == "off":
        return False
    if mode == "always":
        return True
    if mode == "fail":
        return not success
    if not success:
        return True

    key = f"{url}|{reason}".encode("utf-8", errors="ignore")
    bucket = int(hashlib.sha1(key).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < _sample_rate()


def artifact_path(artifact_id: str) -> Path:
    if not _SAFE_ID.fullmatch(artifact_id):
        raise ValueError("invalid artifact_id")
    root = config.ARTIFACT_DIR.resolve()
    target = (root / artifact_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise PermissionError(f"path outside ARTIFACT_DIR: {artifact_id}") from e
    return target


def _file_path(artifact_id: str, file_name: str) -> Path:
    if "/" in file_name or "\\" in file_name or not _SAFE_FILE.fullmatch(file_name):
        raise ValueError("invalid artifact file name")
    root = artifact_path(artifact_id)
    target = (root / file_name).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise PermissionError(f"path outside artifact: {file_name}") from e
    return target


def _new_artifact_id(url: str, reason: str) -> str:
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(f"{url}|{reason}|{uuid.uuid4().hex}".encode()).hexdigest()[:10]
    return f"{now}-{digest}"


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool, int]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False, len(raw)
    return raw[:max_bytes].decode("utf-8", errors="replace"), True, len(raw)


def _json_default(value: Any) -> str:
    return str(value)


def _write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_symlink() or not child.is_file():
                continue
            total += child.stat().st_size
        except OSError:
            continue
    return total


def _artifact_entries() -> tuple[list[dict[str, Any]], int]:
    root = config.ARTIFACT_DIR.resolve()
    if not root.exists():
        return [], 0

    entries: list[dict[str, Any]] = []
    skipped = 0
    for child in root.iterdir():
        try:
            if child.is_symlink() or not child.is_dir():
                skipped += 1
                continue
            if child.parent.resolve() != root:
                skipped += 1
                continue
            artifact_id = child.name
            if not _SAFE_ID.fullmatch(artifact_id):
                skipped += 1
                continue
            metadata_path = child / "metadata.json"
            if not metadata_path.is_file():
                skipped += 1
                continue
            entries.append(
                {
                    "artifact_id": artifact_id,
                    "path": child,
                    "mtime": metadata_path.stat().st_mtime,
                    "size": _dir_size(child),
                }
            )
        except OSError:
            skipped += 1
    return entries, skipped


def save_artifact(
    *,
    url: str,
    reason: str,
    success: bool = False,
    final_url: str = "",
    http_status: int = 0,
    gear_used: str = "",
    session_id: str = "",
    html: str = "",
    screenshot: bytes | None = None,
    console_messages: list[dict[str, Any]] | None = None,
    request_failures: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Persist an artifact directory and return its id, or "" when disabled."""
    if not should_capture(success=success, url=url, reason=reason):
        return ""

    artifact_id = _new_artifact_id(url, reason)
    root = artifact_path(artifact_id)
    root.mkdir(parents=True, exist_ok=False)

    files: list[str] = []
    html_truncated = False
    html_bytes = 0
    if html:
        html_part, html_truncated, html_bytes = _truncate_utf8(
            html,
            int(getattr(config, "ARTIFACT_HTML_MAX_BYTES", 512 * 1024)),
        )
        atomic_write(root / "page.html", html_part)
        files.append("page.html")

    if screenshot:
        atomic_write(root / "screenshot.png", screenshot)
        files.append("screenshot.png")

    console_messages = console_messages or []
    if console_messages:
        _write_json(root / "console.json", console_messages)
        files.append("console.json")

    request_failures = request_failures or []
    if request_failures:
        _write_json(root / "request_failures.json", request_failures)
        files.append("request_failures.json")

    metadata = {
        "artifact_id": artifact_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "url": url,
        "final_url": final_url or url,
        "success": success,
        "reason": reason,
        "http_status": http_status,
        "gear_used": gear_used,
        "session_id": session_id,
        "files": sorted(files),
        "html_truncated": html_truncated,
        "html_bytes": html_bytes,
        "console_count": len(console_messages),
        "request_failure_count": len(request_failures),
        "extra": extra or {},
    }
    _write_json(root / "metadata.json", metadata)
    return artifact_id


def list_artifacts(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    rows: list[dict[str, Any]] = []
    entries, _ = _artifact_entries()
    for entry in sorted(entries, key=lambda item: float(item["mtime"]), reverse=True):
        try:
            meta_path = Path(entry["path"]) / "metadata.json"
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rows.append(data)
        if len(rows) >= limit:
            break
    return rows


def read_artifact(artifact_id: str, file_name: str = "metadata.json") -> str:
    if file_name not in _TEXT_FILES:
        raise ValueError("artifact file is not text-readable")
    path = _file_path(artifact_id, file_name)
    if not path.exists():
        raise FileNotFoundError(f"artifact file not found: {artifact_id}/{file_name}")
    return path.read_text(encoding="utf-8")


def read_artifact_screenshot(artifact_id: str) -> bytes:
    path = _file_path(artifact_id, "screenshot.png")
    if not path.exists():
        raise FileNotFoundError(f"artifact screenshot not found: {artifact_id}/screenshot.png")
    return path.read_bytes()


def artifact_summary(artifact_id: str) -> dict[str, Any]:
    """Return a compact, non-body diagnostic summary for a debug artifact."""
    root = artifact_path(artifact_id)
    metadata_path = root / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"artifact metadata not found: {artifact_id}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("artifact metadata must be a JSON object")

    declared_files = {
        str(file_name)
        for file_name in metadata.get("files", [])
        if isinstance(file_name, str) and _SAFE_FILE.fullmatch(file_name)
    }
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.is_symlink() or not child.is_file() or not _SAFE_FILE.fullmatch(child.name):
            continue
        stat = child.stat()
        total_bytes += stat.st_size
        files.append(
            {
                "name": child.name,
                "size": stat.st_size,
                "declared": child.name in declared_files or child.name == "metadata.json",
                "text_readable": child.name in _TEXT_FILES,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime,
                    UTC,
                ).isoformat(timespec="seconds"),
            }
        )

    extra = metadata.get("extra")
    return {
        "artifact_id": str(metadata.get("artifact_id") or artifact_id),
        "created_at": metadata.get("created_at", ""),
        "url": metadata.get("url", ""),
        "final_url": metadata.get("final_url", ""),
        "success": bool(metadata.get("success", False)),
        "reason": metadata.get("reason", ""),
        "http_status": int(metadata.get("http_status") or 0),
        "gear_used": metadata.get("gear_used", ""),
        "session_id": metadata.get("session_id", ""),
        "html_truncated": bool(metadata.get("html_truncated", False)),
        "html_bytes": int(metadata.get("html_bytes") or 0),
        "console_count": int(metadata.get("console_count") or 0),
        "request_failure_count": int(metadata.get("request_failure_count") or 0),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "declared_files": sorted(declared_files),
        "files": files,
        "extra_keys": sorted(extra) if isinstance(extra, dict) else [],
    }


def artifact_dir_size() -> int:
    """Return total bytes used by valid artifact directories."""
    entries, _ = _artifact_entries()
    return sum(int(entry["size"]) for entry in entries)


def cleanup_artifacts(
    *,
    dry_run: bool = True,
    max_age_days: int | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Delete old or excess debug artifacts.

    Only direct child directories under ARTIFACT_DIR with safe ids and metadata.json
    are eligible. Invalid directories are reported as skipped and never removed.
    Negative age/size limits disable that policy.
    """
    root = config.ARTIFACT_DIR.resolve()
    entries, skipped = _artifact_entries()

    if max_age_days is None:
        max_age_days = int(getattr(config, "ARTIFACT_RETENTION_DAYS", 14))
    if max_total_bytes is None:
        max_total_bytes = int(getattr(config, "ARTIFACT_MAX_BYTES", 512 * 1024 * 1024))

    total_before = sum(int(entry["size"]) for entry in entries)
    delete_reasons: dict[str, set[str]] = {}

    if max_age_days >= 0:
        cutoff = time.time() - (max_age_days * 86400)
        for entry in entries:
            if float(entry["mtime"]) < cutoff:
                delete_reasons.setdefault(str(entry["artifact_id"]), set()).add("age")

    if max_total_bytes >= 0:
        selected_ids = set(delete_reasons)
        remaining = [entry for entry in entries if entry["artifact_id"] not in selected_ids]
        projected_size = sum(int(entry["size"]) for entry in remaining)
        for entry in sorted(remaining, key=lambda item: float(item["mtime"])):
            if projected_size <= max_total_bytes:
                break
            delete_reasons.setdefault(str(entry["artifact_id"]), set()).add("max-bytes")
            projected_size -= int(entry["size"])

    candidates = [
        entry
        for entry in sorted(entries, key=lambda item: float(item["mtime"]))
        if entry["artifact_id"] in delete_reasons
    ]

    deleted_ids: set[str] = set()
    errors: list[dict[str, str]] = []
    if not dry_run:
        for entry in candidates:
            path = Path(entry["path"]).resolve()
            try:
                path.relative_to(root)
                if path.parent != root or not _SAFE_ID.fullmatch(path.name):
                    raise PermissionError(f"refusing to delete unsafe artifact path: {path}")
                shutil.rmtree(path)
                deleted_ids.add(str(entry["artifact_id"]))
            except OSError as e:
                errors.append({"artifact_id": str(entry["artifact_id"]), "error": str(e)})

    candidate_rows = [
        {
            "artifact_id": str(entry["artifact_id"]),
            "size": int(entry["size"]),
            "mtime": datetime.fromtimestamp(
                float(entry["mtime"]), UTC
            ).isoformat(timespec="seconds"),
            "reasons": sorted(delete_reasons[str(entry["artifact_id"])]),
        }
        for entry in candidates
    ]
    candidate_bytes = sum(int(entry["size"]) for entry in candidates)
    deleted_bytes = 0 if dry_run else sum(
        int(entry["size"]) for entry in candidates if entry["artifact_id"] in deleted_ids
    )

    return {
        "dry_run": dry_run,
        "artifact_dir": str(root),
        "max_age_days": max_age_days,
        "max_total_bytes": max_total_bytes,
        "total_before_bytes": total_before,
        "projected_after_bytes": max(0, total_before - candidate_bytes),
        "candidate_count": len(candidates),
        "candidate_bytes": candidate_bytes,
        "deleted_count": 0 if dry_run else len(deleted_ids),
        "deleted_bytes": deleted_bytes,
        "retained_count": max(0, len(entries) - len(candidates)),
        "skipped_count": skipped,
        "candidates": candidate_rows,
        "errors": errors,
    }
