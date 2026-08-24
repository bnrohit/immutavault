from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

TRANSPORT_MARKER = ".immutavault-transport.json"
CHAIN_INDEX = ".immutavault-chain-index.json"
DEFAULT_STATE_ROOT = "/var/lib/immutavault/cbt"
DEPENDENCY_INDEX = "dependencies.json"
SCHEMA = 1


def state_root() -> Path:
    return Path(os.getenv("IMMUTAVAULT_CBT_STATE_DIR", DEFAULT_STATE_ROOT))


def state_path(platform: str, vm_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in platform).strip("._") or "vmware"
    digest = hashlib.sha256(f"{platform}\0{vm_id}".encode()).hexdigest()[:32]
    return state_root() / safe / f"{digest}.json"


def atomic_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def find_transport_marker(root: str | Path) -> Path | None:
    base = Path(root)
    direct = base / TRANSPORT_MARKER
    if direct.is_file():
        return direct
    matches = [p for p in base.rglob(TRANSPORT_MARKER) if ".immutavault-chain" not in p.parts]
    return matches[0] if len(matches) == 1 else None


def marker_for_source(root: str | Path) -> dict[str, Any] | None:
    path = find_transport_marker(root)
    return read_json(path) if path else None


def _dep_path() -> Path:
    return state_root() / DEPENDENCY_INDEX


def _deps() -> dict[str, dict[str, Any]]:
    raw = read_json(_dep_path()) or {}
    rows = raw.get("snapshots") or {}
    return {str(k): dict(v) for k, v in rows.items() if isinstance(v, dict)}


def _save_deps(rows: dict[str, dict[str, Any]]) -> None:
    atomic_json(_dep_path(), {"schema": SCHEMA, "snapshots": rows})


def commit_after_backup(source_path: str | Path, snapshot_id: str) -> None:
    """Advance CBT state only after restic has durably returned a snapshot ID."""
    marker = marker_for_source(source_path)
    if not marker or marker.get("transport") != "vmware-cbt-vddk":
        return
    if int(marker.get("schema", 0)) != SCHEMA:
        raise RuntimeError("unsupported VMware transport marker schema")
    if not bool(marker.get("seeded")):
        return
    platform, vm_id = str(marker.get("platform") or ""), str(marker.get("vm_id") or "")
    if not platform or not vm_id:
        raise RuntimeError("VMware transport marker is missing platform/vm identity")
    disks = list(marker.get("disks") or [])
    fingerprint = str(marker.get("config_fingerprint") or "")
    if not disks or not fingerprint:
        raise RuntimeError("seeded VMware transport marker lacks CBT state")
    path = state_path(platform, vm_id)
    current = read_json(path)
    kind = str(marker.get("kind") or "")
    if kind == "baseline":
        parent = None
        baseline = snapshot_id
        state = {
            "schema": SCHEMA, "transport": "vmware-cbt-vddk", "platform": platform,
            "vm_id": vm_id, "vm_name": str(marker.get("vm_name") or ""),
            "baseline_snapshot_id": snapshot_id, "last_snapshot_id": snapshot_id,
            "chain_snapshot_ids": [snapshot_id], "config_fingerprint": fingerprint, "disks": disks,
        }
    elif kind == "delta":
        if not current:
            raise RuntimeError("cannot commit CBT delta without committed baseline state")
        parent = str(marker.get("parent_snapshot_id") or "")
        baseline = str(marker.get("baseline_snapshot_id") or "")
        if parent != str(current.get("last_snapshot_id") or ""):
            raise RuntimeError("CBT delta parent does not match last committed point")
        if baseline != str(current.get("baseline_snapshot_id") or ""):
            raise RuntimeError("CBT delta baseline does not match committed state")
        chain = [str(x) for x in current.get("chain_snapshot_ids") or []]
        if not chain or chain[-1] != parent:
            raise RuntimeError("CBT chain state is inconsistent")
        state = {**current, "last_snapshot_id": snapshot_id, "chain_snapshot_ids": chain + [snapshot_id],
                 "config_fingerprint": fingerprint, "disks": disks}
    else:
        raise RuntimeError(f"unknown VMware transport marker kind: {kind}")
    atomic_json(path, state)
    rows = _deps()
    rows[snapshot_id] = {"parent": parent, "baseline": baseline, "kind": kind, "platform": platform, "vm_id": vm_id}
    _save_deps(rows)


def expand_dependencies(snapshot_ids: set[str]) -> set[str]:
    rows = _deps(); result: set[str] = set()
    for sid in snapshot_ids:
        current, seen = sid, set()
        while current and current not in seen:
            seen.add(current)
            row = rows.get(current)
            if not row:
                break
            parent = str(row.get("parent") or "")
            baseline = str(row.get("baseline") or "")
            nxt = parent or (baseline if baseline != current else "")
            if nxt:
                result.add(nxt)
            current = nxt
    return result


def chain_for(snapshot_id: str, max_depth: int = 256) -> list[str]:
    rows = _deps(); order: list[str] = []; current = snapshot_id; seen: set[str] = set()
    while current:
        if current in seen:
            raise RuntimeError("VMware CBT dependency loop detected")
        seen.add(current); order.append(current)
        if len(order) > max_depth:
            raise RuntimeError("VMware CBT chain exceeds safety depth")
        row = rows.get(current)
        if not row:
            break
        current = str(row.get("parent") or "")
    order.reverse()
    return order


def all_dependency_ancestors() -> set[str]:
    rows = _deps()
    return expand_dependencies(set(rows))


def prune_dependencies(existing_snapshot_ids: set[str]) -> None:
    rows = _deps(); trimmed = {sid: row for sid, row in rows.items() if sid in existing_snapshot_ids}
    if trimmed != rows:
        _save_deps(trimmed)
