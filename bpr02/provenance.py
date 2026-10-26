"""Dataset fingerprinting and atomic run-manifest writing for reproducible research."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 fingerprint of a file without loading it all into memory."""
    source = Path(path)
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    digest = hashlib.sha256()
    with source.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    """Convert common pandas/numpy values to JSON-safe built-in values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        return _json_safe(value.item())
    return str(value)


def build_run_manifest(
    *,
    source_path: str | Path,
    mode: str,
    parameters: Mapping[str, Any],
    outputs: Mapping[str, str | Path],
    rows: int | None = None,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    summary: Mapping[str, Any] | None = None,
    notes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a manifest identifying the exact input file and the run settings."""
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("mode must be a non-empty string")
    source = Path(source_path)
    if rows is not None and rows < 0:
        raise ValueError("rows must be >= 0 when provided")

    return {
        "manifest_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "source": {
            "file_name": source.name,
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
        },
        "dataset": {
            "rows": rows,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "timestamps_normalized_to_utc": True,
        },
        "parameters": _json_safe(parameters),
        "outputs": {str(key): str(value) for key, value in outputs.items()},
        "summary": _json_safe(summary or {}),
        "notes": list(notes),
    }


def write_run_manifest(
    destination: str | Path,
    manifest: Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
) -> Path:
    """Write a JSON manifest atomically without overwriting input/output artifacts."""
    target = Path(destination)
    target_resolved = target.resolve()

    protected_paths: list[Path] = []
    if source_path is not None:
        protected_paths.append(Path(source_path).resolve())
    for output_path in manifest.get("outputs", {}).values():
        protected_paths.append(Path(output_path).resolve())

    if target_resolved in protected_paths:
        raise ValueError("manifest output must not overwrite the source data or a run output")

    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        _json_safe(manifest), indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False
    ) + "\n"

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, target)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return target
