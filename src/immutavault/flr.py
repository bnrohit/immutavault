from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import threading
import time
from typing import Any, BinaryIO, Callable
import uuid

from .config import Config
from .restic import ResticRepository
from .runner import run
from .util import safe_component


DISK_SUFFIXES = (".vmdk", ".vhd", ".vhdx", ".qcow2", ".raw", ".img")
VMDK_EXTENT_SUFFIXES = ("-flat.vmdk", "-delta.vmdk", "-sesparse.vmdk", "-ctk.vmdk")
LAYOUT_FILE = "immutavault-vddk-layout.json"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = default if raw in (None, "") else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class FLRSettings:
    """Runtime policy for v0.8 file-level recovery.

    FLR is intentionally operational rather than backup-policy configuration:
    it controls temporary FUSE/libguestfs sessions on the vault. Values are
    environment-backed so the same immutable YAML can be used by controller and
    portal services without placing host-specific mount policy in a backup job.
    """

    enabled: bool = True
    mount_root: str = "/srv/immutavault/flr"
    session_ttl_minutes: int = 30
    max_download_bytes: int = 5 * 1024 * 1024 * 1024
    max_sessions_per_user: int = 2
    max_disks: int = 16
    mount_wait_seconds: int = 30

    @classmethod
    def from_environment(cls) -> "FLRSettings":
        mount_root = os.getenv("IMMUTAVAULT_FLR_MOUNT_ROOT", "/srv/immutavault/flr").strip()
        if not mount_root.startswith("/"):
            raise ValueError("IMMUTAVAULT_FLR_MOUNT_ROOT must be an absolute path")
        return cls(
            enabled=_env_bool("IMMUTAVAULT_FLR_ENABLED", True),
            mount_root=mount_root,
            session_ttl_minutes=_env_int("IMMUTAVAULT_FLR_SESSION_TTL_MINUTES", 30, minimum=1, maximum=240),
            max_download_bytes=_env_int(
                "IMMUTAVAULT_FLR_MAX_DOWNLOAD_BYTES",
                5 * 1024 * 1024 * 1024,
                minimum=1,
                maximum=1024 * 1024 * 1024 * 1024,
            ),
            max_sessions_per_user=_env_int("IMMUTAVAULT_FLR_MAX_SESSIONS_PER_USER", 2, minimum=1, maximum=16),
            max_disks=_env_int("IMMUTAVAULT_FLR_MAX_DISKS", 16, minimum=1, maximum=64),
            mount_wait_seconds=_env_int("IMMUTAVAULT_FLR_MOUNT_WAIT_SECONDS", 30, minimum=1, maximum=180),
        )


@dataclass
class FLRSession:
    session_id: str
    owner: str
    snapshot_id: str
    platform: str
    vm_name: str
    source_path: str
    root: Path
    restic_mount: Path
    guest_mount: Path
    created_at: datetime
    expires_at: datetime
    disks: list[str] = field(default_factory=list)
    restic_process: subprocess.Popen[str] | None = None
    log_handle: Any = None

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "platform": self.platform,
            "vm_name": self.vm_name,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "disks": list(self.disks),
            "read_only": True,
        }


@dataclass(frozen=True)
class FLRFile:
    path: Path
    name: str
    size: int


class FLRManager:
    """Create short-lived read-only guest filesystem views for file recovery.

    The primary repository stays immutable. restic is mounted read-only with
    --no-lock, and libguestfs/guestmount is always invoked with --ro. The portal
    never receives a path that can escape the mounted guest root and never
    follows guest symlinks for browse/download operations.
    """

    def __init__(
        self,
        cfg: Config,
        repo: ResticRepository,
        *,
        settings: FLRSettings | None = None,
        command_runner: Callable[..., Any] = run,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        if settings is not None:
            self.settings = settings
        elif hasattr(cfg, "flr"):
            self.settings = FLRSettings(
                enabled=cfg.flr.enabled,
                mount_root=cfg.flr.mount_root,
                session_ttl_minutes=cfg.flr.session_ttl_minutes,
                max_download_bytes=cfg.flr.max_download_bytes,
                max_sessions_per_user=cfg.flr.max_sessions_per_user,
                max_disks=cfg.flr.max_disks,
                mount_wait_seconds=cfg.flr.mount_wait_seconds,
            )
        else:
            self.settings = FLRSettings.from_environment()
        self._run = command_runner
        self._popen = popen_factory
        self._which = which
        self._sleep = sleeper
        self._sessions: dict[str, FLRSession] = {}
        self._lock = threading.RLock()

    def prerequisite_problems(self) -> list[str]:
        if not self.settings.enabled:
            return []
        problems: list[str] = []
        for name in ("restic", "guestmount", "guestunmount"):
            if not self._which(name):
                problems.append(f"FLR requires {name} but it is not installed")
        if not (self._which("fusermount3") or self._which("fusermount")):
            problems.append("FLR requires fusermount3/fusermount but neither is installed")
        fuse = Path("/dev/fuse")
        if not fuse.exists():
            problems.append("FLR requires /dev/fuse")
        elif not os.access(fuse, os.R_OK | os.W_OK):
            problems.append("FLR service identity cannot read/write /dev/fuse")
        return problems

    def status(self) -> dict[str, Any]:
        self.cleanup_expired()
        with self._lock:
            active = len(self._sessions)
        return {
            "enabled": self.settings.enabled,
            "active_sessions": active,
            "session_ttl_minutes": self.settings.session_ttl_minutes,
            "max_download_bytes": self.settings.max_download_bytes,
            "prerequisite_problems": self.prerequisite_problems(),
        }

    def _root(self) -> Path:
        return Path(self.settings.mount_root)

    def _session_root(self, session_id: str) -> Path:
        return self._root() / safe_component(session_id)

    def _merged_repo_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.repo._env(local=False))
        return env

    def cleanup_expired(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = [sid for sid, session in self._sessions.items() if session.expires_at <= now]
        for sid in expired:
            self.close_session(sid, force=True)

        root = self._root()
        if not root.exists():
            return
        ttl_seconds = self.settings.session_ttl_minutes * 60
        for child in root.iterdir():
            if not child.is_dir():
                continue
            with self._lock:
                if child.name in self._sessions:
                    continue
            try:
                age = time.time() - child.stat().st_mtime
            except OSError:
                continue
            if age > ttl_seconds:
                self._cleanup_mount_paths(child / "guest", child / "restic")
                shutil.rmtree(child, ignore_errors=True)

    def _wait_for(self, predicate: Callable[[], bool], *, seconds: int, process: subprocess.Popen[str] | None = None) -> bool:
        deadline = time.monotonic() + max(1, seconds)
        while time.monotonic() < deadline:
            if predicate():
                return True
            if process is not None and process.poll() is not None:
                return False
            self._sleep(0.1)
        return predicate()

    @staticmethod
    def _source_inside_snapshot(snapshot_root: Path, source_path: str) -> Path:
        rel = PurePosixPath(source_path)
        parts = [part for part in rel.parts if part not in {"/", "", "."}]
        if ".." in parts:
            raise ValueError("recovery point source path is unsafe")
        return snapshot_root.joinpath(*parts)

    def _discover_disk_images(self, source: Path) -> list[Path]:
        candidates: list[Path] = []
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            lower = path.name.lower()
            if not lower.endswith(DISK_SUFFIXES):
                continue
            if lower.endswith(VMDK_EXTENT_SUFFIXES):
                continue
            candidates.append(path)

        layout = source / LAYOUT_FILE
        if layout.is_file():
            try:
                value = json.loads(layout.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = {}
            declared = value.get("flr_disk_images") or value.get("flr_disks") or []
            for item in declared:
                raw = item.get("path") if isinstance(item, dict) else item
                if not raw:
                    continue
                rel = PurePosixPath(str(raw))
                if rel.is_absolute() or ".." in rel.parts:
                    continue
                candidate = source.joinpath(*rel.parts)
                if candidate.is_file():
                    candidates.append(candidate)

        unique = sorted({str(p): p for p in candidates}.values(), key=lambda p: str(p))
        return unique[: self.settings.max_disks]

    def _start_restic_mount(self, mountpoint: Path, log_handle: Any) -> subprocess.Popen[str]:
        restic = self._which("restic") or "restic"
        return self._popen(
            [restic, "mount", "--no-lock", str(mountpoint)],
            env=self._merged_repo_env(),
            stdout=log_handle,
            stderr=log_handle,
            text=True,
            start_new_session=True,
        )

    def _mount_guest(self, disks: list[Path], guest_mount: Path) -> None:
        guestmount = self._which("guestmount") or "guestmount"
        command = [guestmount, "--ro"]
        for disk in disks:
            command += ["-a", str(disk)]
        command += ["-i", str(guest_mount)]
        result = self._run(command, timeout=min(self.cfg.runtime.command_timeout_seconds, 600), check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "guestmount failed").strip()
            raise RuntimeError("FLR could not inspect/mount the guest filesystem read-only: " + detail)

    def _snapshot_root(self, restic_mount: Path, snapshot_id: str) -> Path:
        exact = restic_mount / "ids" / snapshot_id
        if exact.exists():
            return exact
        ids = restic_mount / "ids"
        if ids.is_dir():
            matches = [p for p in ids.iterdir() if p.name.startswith(snapshot_id)]
            if len(matches) == 1:
                return matches[0]
        return exact

    def open_session(self, point: dict[str, Any], *, actor: str) -> dict[str, Any]:
        if not self.settings.enabled:
            raise RuntimeError("file-level recovery is disabled by IMMUTAVAULT_FLR_ENABLED")
        problems = self.prerequisite_problems()
        if problems:
            raise RuntimeError("; ".join(problems))
        self.cleanup_expired()

        with self._lock:
            current = sum(1 for s in self._sessions.values() if s.owner == actor)
            if current >= self.settings.max_sessions_per_user:
                raise RuntimeError(f"FLR session limit reached for {actor}: {self.settings.max_sessions_per_user}")

        session_id = uuid.uuid4().hex
        root = self._session_root(session_id)
        restic_mount = root / "restic"
        guest_mount = root / "guest"
        root.mkdir(parents=True, exist_ok=False)
        restic_mount.mkdir(mode=0o700)
        guest_mount.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        log_handle = (root / "flr.log").open("a", encoding="utf-8")
        process: subprocess.Popen[str] | None = None
        try:
            process = self._start_restic_mount(restic_mount, log_handle)
            snapshot_root = self._snapshot_root(restic_mount, str(point["snapshot_id"]))
            ready = self._wait_for(
                snapshot_root.exists,
                seconds=self.settings.mount_wait_seconds,
                process=process,
            )
            if not ready:
                raise RuntimeError("restic FUSE mount did not expose the requested recovery point")
            source = self._source_inside_snapshot(snapshot_root, str(point["source_path"]))
            if not source.exists():
                raise RuntimeError(f"FLR source path is missing inside recovery point: {point['source_path']}")
            disks = self._discover_disk_images(source)
            if not disks:
                if (source / LAYOUT_FILE).is_file():
                    raise RuntimeError(
                        "this native incremental recovery point does not expose standard read-only disk images for FLR; "
                        "upgrade/configure the authorized VDDK helper to publish flr_disk_images in its layout"
                    )
                raise RuntimeError(
                    "no directly mountable guest disk image was found in this recovery point; "
                    "FLR currently requires VMDK/VHD/VHDX/QCOW2/RAW/IMG or a VDDK layout that declares flr_disk_images"
                )
            self._mount_guest(disks, guest_mount)
            if not self._wait_for(lambda: guest_mount.exists() and any(guest_mount.iterdir()), seconds=10):
                raise RuntimeError("guest filesystem mount completed but no browsable filesystem appeared")

            now = datetime.now(timezone.utc)
            session = FLRSession(
                session_id=session_id,
                owner=actor,
                snapshot_id=str(point["snapshot_id"]),
                platform=str(point["platform"]),
                vm_name=str(point["vm_name"]),
                source_path=str(point["source_path"]),
                root=root,
                restic_mount=restic_mount,
                guest_mount=guest_mount,
                created_at=now,
                expires_at=now + timedelta(minutes=self.settings.session_ttl_minutes),
                disks=[str(p.relative_to(source)) for p in disks],
                restic_process=process,
                log_handle=log_handle,
            )
            with self._lock:
                self._sessions[session_id] = session
            return session.public()
        except Exception:
            self._cleanup_mount_paths(guest_mount, restic_mount)
            if process is not None:
                self._stop_process(process)
            log_handle.close()
            shutil.rmtree(root, ignore_errors=True)
            raise

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except (OSError, ProcessLookupError):
            pass

    def _cleanup_mount_paths(self, guest: Path, restic: Path) -> None:
        guestunmount = self._which("guestunmount")
        if guestunmount and guest.exists():
            self._run([guestunmount, str(guest)], timeout=30, check=False)
        fuse = self._which("fusermount3") or self._which("fusermount")
        if fuse:
            if guest.exists():
                self._run([fuse, "-u", str(guest)], timeout=30, check=False)
            if restic.exists():
                self._run([fuse, "-u", str(restic)], timeout=30, check=False)

    def close_session(self, session_id: str, *, actor: str | None = None, force: bool = False) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                if force:
                    root = self._session_root(session_id)
                    self._cleanup_mount_paths(root / "guest", root / "restic")
                    shutil.rmtree(root, ignore_errors=True)
                    return
                raise ValueError("FLR session not found")
            if not force and actor is not None and session.owner != actor:
                raise PermissionError("FLR session belongs to another portal user")
            self._sessions.pop(session_id, None)

        self._cleanup_mount_paths(session.guest_mount, session.restic_mount)
        if session.restic_process is not None:
            self._stop_process(session.restic_process)
        if session.log_handle is not None:
            try:
                session.log_handle.close()
            except OSError:
                pass
        shutil.rmtree(session.root, ignore_errors=True)

    def _session(self, session_id: str, *, actor: str, admin: bool = False) -> FLRSession:
        self.cleanup_expired()
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise ValueError("FLR session not found or expired")
        if not admin and session.owner != actor:
            raise PermissionError("FLR session belongs to another portal user")
        return session

    @staticmethod
    def _relative_parts(user_path: str) -> tuple[str, ...]:
        if "\x00" in user_path:
            raise ValueError("invalid path")
        path = PurePosixPath(user_path or "/")
        parts = tuple(part for part in path.parts if part not in {"/", "", "."})
        if any(part == ".." for part in parts):
            raise ValueError("path traversal is not allowed")
        return parts

    @staticmethod
    def _safe_guest_path(root: Path, parts: tuple[str, ...]) -> Path:
        current = root
        for part in parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                raise ValueError("path not found")
            if stat.S_ISLNK(info.st_mode):
                raise PermissionError("FLR does not follow guest symlinks")
        return current

    def list_directory(self, session_id: str, user_path: str, *, actor: str, admin: bool = False) -> dict[str, Any]:
        session = self._session(session_id, actor=actor, admin=admin)
        parts = self._relative_parts(user_path)
        directory = self._safe_guest_path(session.guest_mount, parts)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("requested FLR path is not a directory")
        entries: list[dict[str, Any]] = []
        with os.scandir(directory) as scan:
            for entry in scan:
                try:
                    item = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                kind = (
                    "symlink" if stat.S_ISLNK(item.st_mode) else
                    "directory" if stat.S_ISDIR(item.st_mode) else
                    "file" if stat.S_ISREG(item.st_mode) else "special"
                )
                entries.append({
                    "name": entry.name,
                    "type": kind,
                    "size": item.st_size if kind == "file" else None,
                    "modified": datetime.fromtimestamp(item.st_mtime, tz=timezone.utc).isoformat(),
                    "downloadable": kind == "file" and item.st_size <= self.settings.max_download_bytes,
                })
        entries.sort(key=lambda x: (x["type"] != "directory", x["name"].casefold()))
        normalized = "/" + "/".join(parts) if parts else "/"
        return {"session": session.public(), "path": normalized, "entries": entries}

    def open_file(self, session_id: str, user_path: str, *, actor: str, admin: bool = False) -> FLRFile:
        session = self._session(session_id, actor=actor, admin=admin)
        parts = self._relative_parts(user_path)
        if not parts:
            raise ValueError("a file path is required")
        path = self._safe_guest_path(session.guest_mount, parts)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("FLR download path is not a regular file")
        if info.st_size > self.settings.max_download_bytes:
            raise ValueError(
                f"file is {info.st_size} bytes, above configured FLR download limit {self.settings.max_download_bytes}"
            )
        return FLRFile(path=path, name=parts[-1], size=info.st_size)

    @staticmethod
    def stream_file(file: FLRFile, writer: BinaryIO, *, chunk_size: int = 1024 * 1024) -> None:
        with file.path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                writer.write(chunk)
