from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform as host_platform
import shutil
import ssl
import subprocess
import tempfile
import time
from typing import Any, Iterator
from urllib.parse import urlparse
import uuid

from .base import VM
from .vmware import VMwareAdapter
from immutavault.runner import run
from immutavault.transport_state import TRANSPORT_MARKER, atomic_json, read_json, state_path
from immutavault.util import safe_component

SCHEMA = 1
SECTOR = 512
DEFAULT_VDDK_DIR = "/opt/vmware-vix-disklib-distrib"


class IncrementalUnavailable(RuntimeError):
    pass


def change_epoch(value: str) -> str:
    return str(value).split("/", 1)[0]


def normalize_extents(extents: list[tuple[int, int]], capacity: int) -> list[dict[str, int]]:
    spans: list[tuple[int, int]] = []
    for start, length in extents:
        if length <= 0:
            continue
        lo = max(0, int(start) // SECTOR * SECTOR)
        raw_hi = min(int(capacity), int(start) + int(length))
        hi = min(int(capacity), (raw_hi + SECTOR - 1) // SECTOR * SECTOR)
        if hi > lo:
            spans.append((lo, hi))
    spans.sort()
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [{"start": lo, "length": hi - lo} for lo, hi in merged]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def find_chain_index(source: Path) -> Path | None:
    current = source.resolve()
    for parent in [current, *current.parents]:
        path = parent / ".immutavault-chain-index.json"
        if path.is_file():
            return path
    return None


class VMwareIncrementalAdapter(VMwareAdapter):
    """Prefer native VMware CBT/VDDK incrementals with safe hot-clone fallback."""

    INCREMENTAL = {"auto", "cbt", "vddk", "vddk-cbt", "incremental"}
    STRICT = {"vddk-cbt-strict", "cbt-strict"}
    LEGACY = {"hot-clone-export", "snapshot-clone-export", "hot", "export", "cold-export"}

    def _base(self) -> VMwareAdapter:
        return VMwareAdapter(replace(self.cfg, mode="hot-clone-export"), self.timeout)

    def _fallback_allowed(self) -> bool:
        return bool(self.cfg.options.get("incremental_fallback", True)) and self.cfg.mode.lower() not in self.STRICT

    def _libdir(self) -> str:
        return str(self.cfg.options.get("vddk_libdir") or DEFAULT_VDDK_DIR)

    def _thumbprint(self) -> str:
        direct = str(self.cfg.options.get("vddk_thumbprint") or "").strip()
        env_name = str(self.cfg.options.get("vddk_thumbprint_env") or "").strip()
        return direct or (os.getenv(env_name, "").strip() if env_name else "")

    def _transports(self) -> str:
        value = self.cfg.options.get("vddk_transports", "san:hotadd:nbdssl:nbd")
        if isinstance(value, list):
            return ":".join(str(x).strip() for x in value if str(x).strip())
        return str(value).strip() or "san:hotadd:nbdssl:nbd"

    def _vddk_problems(self) -> list[str]:
        problems: list[str] = []
        if host_platform.machine().lower() not in {"x86_64", "amd64"}:
            problems.append("VDDK requires an x86-64 backup proxy")
        for binary, text in (("nbdkit", "nbdkit is not installed"), ("nbdsh", "nbdsh/libnbd is not installed")):
            if shutil.which(binary) is None:
                problems.append(text)
        try:
            import pyVmomi  # noqa: F401
        except Exception:
            problems.append("pyVmomi is not installed")
        libdir = Path(self._libdir())
        if not libdir.is_dir():
            problems.append(f"VDDK library directory is missing: {libdir}")
        if not self._thumbprint():
            problems.append("VDDK TLS thumbprint is not configured")
        if shutil.which("nbdkit") and libdir.is_dir():
            probe = run(["nbdkit", "vddk", "--dump-plugin", f"libdir={libdir}"], timeout=60, check=False)
            text = probe.stdout + probe.stderr
            if probe.returncode != 0 or "VixDiskLib_Open=1" not in text:
                problems.append("nbdkit VDDK plugin cannot load a usable VDDK library")
        return problems

    def doctor(self) -> list[str]:
        mode = self.cfg.mode.lower()
        if mode not in self.INCREMENTAL | self.STRICT | self.LEGACY:
            return [f"unsupported VMware backup mode: {self.cfg.mode}"]
        problems = self._base().doctor()
        if mode in self.STRICT:
            problems.extend(self._vddk_problems())
        return problems

    def platform_info(self) -> dict[str, Any]:
        info = self._base().platform_info()
        requested = self.cfg.mode.lower() in self.INCREMENTAL | self.STRICT
        problems = self._vddk_problems() if requested else []
        info["backup_transport"] = {
            "configured_mode": self.cfg.mode,
            "incremental_requested": requested,
            "incremental_available": requested and not problems,
            "fallback": "hot-clone-export" if self._fallback_allowed() else None,
            "vddk_transports": self._transports() if requested else None,
            "problems": problems,
        }
        return info

    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        mode = self.cfg.mode.lower()
        if mode in self.LEGACY:
            return super().export(vm, destination, dry_run=dry_run)
        if mode not in self.INCREMENTAL | self.STRICT:
            raise ValueError(f"unsupported VMware backup mode: {self.cfg.mode}")
        if dry_run:
            return destination / safe_component(vm.name)
        state = read_json(state_path(self.cfg.name, vm.id))
        try:
            if not state:
                return self._baseline(vm, destination, "initial CBT baseline")
            max_chain = max(2, int(self.cfg.options.get("cbt_max_chain_length", 32)))
            if len(state.get("chain_snapshot_ids") or []) >= max_chain:
                return self._baseline(vm, destination, f"scheduled full after {max_chain} points")
            problems = self._vddk_problems()
            if problems:
                raise IncrementalUnavailable("; ".join(problems))
            return self._delta(vm, destination, state)
        except IncrementalUnavailable as exc:
            if not self._fallback_allowed():
                raise
            return self._baseline(vm, destination, str(exc))
        except Exception as exc:
            if not self._fallback_allowed():
                raise
            return self._baseline(vm, destination, f"incremental error: {type(exc).__name__}: {exc}")

    def _endpoint(self) -> tuple[str, int]:
        raw = self.cfg.endpoint if "://" in self.cfg.endpoint else "https://" + self.cfg.endpoint
        parsed = urlparse(raw)
        if not parsed.hostname:
            raise IncrementalUnavailable(f"cannot parse VMware endpoint: {self.cfg.endpoint}")
        return parsed.hostname, parsed.port or 443

    @contextmanager
    def _vsphere(self) -> Iterator[Any]:
        try:
            from pyVim.connect import SmartConnect, Disconnect
        except Exception as exc:
            raise IncrementalUnavailable("pyVmomi is not installed") from exc
        env = self._govc_env(); host, port = self._endpoint()
        context = ssl._create_unverified_context() if bool(self.cfg.options.get("insecure", False)) else ssl.create_default_context(cafile=str(self.cfg.options.get("tls_ca_certs")) if self.cfg.options.get("tls_ca_certs") else None)
        try:
            si = SmartConnect(host=host, user=env["GOVC_USERNAME"], pwd=env["GOVC_PASSWORD"], port=port, sslContext=context)
        except Exception as exc:
            raise IncrementalUnavailable(f"pyVmomi cannot connect to {host}: {exc}") from exc
        try:
            yield si
        finally:
            try: Disconnect(si)
            except Exception: pass

    @staticmethod
    def _wait(task: Any, timeout: int) -> Any:
        deadline = time.monotonic() + max(30, timeout)
        while time.monotonic() < deadline:
            state = str(getattr(task.info, "state", ""))
            if state.endswith("success") or state == "success":
                return task.info.result
            if state.endswith("error") or state == "error":
                err = getattr(task.info, "error", None)
                raise RuntimeError(str(getattr(err, "msg", err) or "vSphere task failed"))
            time.sleep(0.5)
        raise TimeoutError("vSphere task timed out")

    def _find_vm(self, si: Any, vm: VM) -> Any:
        from pyVmomi import vim
        obj = si.content.searchIndex.FindByInventoryPath(vm.id)
        if obj is not None:
            return obj
        view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vim.VirtualMachine], True)
        try:
            matches = [x for x in view.view if str(getattr(x, "name", "")) == vm.name]
        finally:
            view.Destroy()
        if len(matches) != 1:
            raise IncrementalUnavailable(f"expected one VM {vm.name!r}, found {len(matches)}")
        return matches[0]

    def _find_vm_name(self, si: Any, name: str) -> Any:
        return self._find_vm(si, VM(id="", name=name))

    def _ensure_cbt(self, vm_obj: Any) -> None:
        if not bool(getattr(getattr(vm_obj, "capability", None), "changeTrackingSupported", False)):
            raise IncrementalUnavailable("VM does not support CBT")
        if bool(getattr(getattr(vm_obj, "config", None), "changeTrackingEnabled", False)):
            return
        if not bool(self.cfg.options.get("cbt_auto_enable", False)):
            raise IncrementalUnavailable("CBT is disabled on the VM")
        from pyVmomi import vim
        self._wait(vm_obj.ReconfigVM_Task(spec=vim.vm.ConfigSpec(changeTrackingEnabled=True)), self.timeout)

    def _snapshot(self, vm_obj: Any, name: str) -> Any:
        quiesce = bool(self.cfg.options.get("quiesce", True)); fallback = bool(self.cfg.options.get("quiesce_fallback_crash_consistent", False))
        try:
            return self._wait(vm_obj.CreateSnapshot_Task(name=name, description="Immutavault CBT/VDDK", memory=False, quiesce=quiesce), self.timeout)
        except Exception as exc:
            if not (quiesce and fallback):
                raise IncrementalUnavailable(f"snapshot failed: {exc}") from exc
            return self._wait(vm_obj.CreateSnapshot_Task(name=name, description="Immutavault crash-consistent CBT/VDDK", memory=False, quiesce=False), self.timeout)

    def _remove_snapshot(self, snapshot: Any) -> None:
        try: task = snapshot.RemoveSnapshot_Task(removeChildren=False, consolidate=True)
        except TypeError: task = snapshot.RemoveSnapshot_Task(removeChildren=False)
        self._wait(task, self.timeout)

    @staticmethod
    def _disks(vm_obj: Any, snapshot: Any | None) -> list[dict[str, Any]]:
        from pyVmomi import vim
        current = [d for d in getattr(vm_obj.config.hardware, "device", []) or [] if isinstance(d, vim.vm.device.VirtualDisk)]
        snap = {}
        if snapshot is not None:
            snap = {int(d.key): d for d in getattr(snapshot.config.hardware, "device", []) or [] if isinstance(d, vim.vm.device.VirtualDisk)}
        current.sort(key=lambda d: (int(getattr(d, "controllerKey", 0)), int(getattr(d, "unitNumber", -1) or -1), int(d.key)))
        rows = []
        for ordinal, d in enumerate(current):
            backing = getattr(d, "backing", None); sback = getattr(snap.get(int(d.key)), "backing", None)
            if str(getattr(backing, "diskMode", "")).startswith("independent"):
                raise IncrementalUnavailable(f"disk {ordinal} is independent")
            if "sharingMultiWriter" in str(getattr(backing, "sharing", "")):
                raise IncrementalUnavailable(f"disk {ordinal} uses multi-writer")
            capacity = int(getattr(d, "capacityInBytes", 0) or int(getattr(d, "capacityInKB", 0) or 0) * 1024)
            rows.append({
                "ordinal": ordinal, "key": int(d.key), "controller_key": int(getattr(d, "controllerKey", 0)),
                "unit_number": int(getattr(d, "unitNumber", -1) or -1), "capacity": capacity,
                "file_name": str(getattr(backing, "fileName", "") or ""),
                "change_id": str(getattr(sback, "changeId", "") or ""),
            })
        if not rows or any(not x["file_name"] for x in rows):
            raise IncrementalUnavailable("VM has no supported VMDK backing")
        return rows

    @staticmethod
    def _fingerprint(vm_obj: Any, disks: list[dict[str, Any]]) -> str:
        from pyVmomi import vim
        nics = []
        for d in getattr(vm_obj.config.hardware, "device", []) or []:
            if isinstance(d, vim.vm.device.VirtualEthernetCard):
                backing = getattr(d, "backing", None)
                nics.append({"key": int(d.key), "mac": str(getattr(d, "macAddress", "") or ""), "network": str(getattr(backing, "deviceName", "") or getattr(backing, "port", "") or "")})
        payload = {
            "cpu": int(getattr(vm_obj.config.hardware, "numCPU", 0) or 0),
            "memory_mb": int(getattr(vm_obj.config.hardware, "memoryMB", 0) or 0),
            "firmware": str(getattr(vm_obj.config, "firmware", "") or ""),
            "guest_id": str(getattr(vm_obj.config, "guestId", "") or ""),
            "disks": [{k: x[k] for k in ("ordinal", "key", "controller_key", "unit_number", "capacity")} for x in disks],
            "nics": nics,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _baseline(self, vm: VM, destination: Path, reason: str) -> Path:
        out = destination / safe_component(vm.name); out.mkdir(parents=True, exist_ok=True)
        env = self._govc_env(); nonce = uuid.uuid4().hex[:12]; snap_name = f"immutavault-v07-{nonce}"; clone = f"immutavault-{safe_component(vm.name)[:42]}-{nonce}"
        snapshot = None; clone_created = False; marker = {
            "schema": SCHEMA, "transport": "vmware-cbt-vddk", "kind": "baseline", "seeded": False,
            "platform": self.cfg.name, "vm_id": vm.id, "vm_name": vm.name, "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(), "parent_snapshot_id": None,
            "baseline_snapshot_id": None, "restore_mode": "ovf", "disks": [],
        }
        active_error = False
        try:
            try:
                with self._vsphere() as si:
                    obj = self._find_vm(si, vm)
                    cbt = True
                    try: self._ensure_cbt(obj)
                    except IncrementalUnavailable: cbt = False
                    snapshot = self._snapshot(obj, snap_name)
                    if cbt:
                        disks = self._disks(obj, snapshot)
                        if all(x["change_id"] for x in disks):
                            marker.update({"seeded": True, "disks": disks, "config_fingerprint": self._fingerprint(obj, disks)})
            except IncrementalUnavailable:
                out = self._base().export(vm, destination)
                atomic_json(destination / TRANSPORT_MARKER, marker)
                return out
            run(["govc", "vm.clone", "-vm", vm.id, "-snapshot", snap_name, "-on=false", clone], timeout=self.timeout, env=env)
            clone_created = True
            run(["govc", "export.ovf", "-vm", clone, str(out)], timeout=self.timeout, env=env)
            atomic_json(destination / TRANSPORT_MARKER, marker)
            return out
        except Exception:
            active_error = True; raise
        finally:
            errors = []
            if clone_created:
                r = run(["govc", "vm.destroy", clone], timeout=self.timeout, env=env, check=False)
                if r.returncode: errors.append("temporary clone cleanup failed")
            if snapshot is not None:
                try: self._remove_snapshot(snapshot)
                except Exception as exc: errors.append(f"snapshot cleanup failed: {exc}")
            if errors and not active_error:
                raise RuntimeError("; ".join(errors))

    def _validate(self, state: dict[str, Any], disks: list[dict[str, Any]], fingerprint: str) -> None:
        if str(state.get("config_fingerprint") or "") != fingerprint:
            raise IncrementalUnavailable("VM hardware/network configuration changed")
        old = {int(x.get("ordinal", -1)): x for x in state.get("disks") or []}
        if len(old) != len(disks): raise IncrementalUnavailable("VM disk count changed")
        for disk in disks:
            previous = old.get(int(disk["ordinal"]))
            if not previous or int(previous.get("capacity", -1)) != int(disk["capacity"]):
                raise IncrementalUnavailable(f"disk {disk['ordinal']} layout changed")
            old_id, new_id = str(previous.get("change_id") or ""), str(disk.get("change_id") or "")
            if not old_id or not new_id or change_epoch(old_id) != change_epoch(new_id):
                raise IncrementalUnavailable(f"disk {disk['ordinal']} CBT epoch is invalid")

    def _query(self, vm_obj: Any, snapshot: Any, disk: dict[str, Any], previous: str) -> list[dict[str, int]]:
        capacity = int(disk["capacity"]); cursor = 0; extents: list[tuple[int, int]] = []
        while cursor < capacity:
            try: info = vm_obj.QueryChangedDiskAreas(snapshot, int(disk["key"]), cursor, previous)
            except Exception as exc: raise IncrementalUnavailable(f"QueryChangedDiskAreas failed: {exc}") from exc
            for area in getattr(info, "changedArea", []) or []:
                extents.append((int(getattr(area, "start", getattr(area, "startOffset", 0))), int(getattr(area, "length", 0))))
            start = int(getattr(info, "startOffset", cursor) or cursor); length = int(getattr(info, "length", 0) or 0)
            if start + length <= cursor: break
            cursor = start + length
        return normalize_extents(extents, capacity)

    @contextmanager
    def _nbd(self, vm_moref: str, disk_file: str, snapshot_moref: str | None, read_only: bool) -> Iterator[str]:
        host, port = self._endpoint(); env = self._govc_env(); thumb = self._thumbprint()
        if not thumb: raise IncrementalUnavailable("VDDK TLS thumbprint is not configured")
        root = Path(tempfile.mkdtemp(prefix="immutavault-vddk-")); os.chmod(root, 0o700)
        password = root / "password"; password.write_text(env["GOVC_PASSWORD"], encoding="utf-8"); os.chmod(password, 0o600)
        sock = root / "disk.sock"
        cmd = ["nbdkit", "--foreground", "--exit-with-parent", "-U", str(sock)] + (["-r"] if read_only else []) + [
            "vddk", f"libdir={self._libdir()}", f"server={host}", f"port={port}", f"user={env['GOVC_USERNAME']}",
            f"password=+{password}", f"thumbprint={thumb}", f"vm=moref={vm_moref}", f"transports={self._transports()}", "unbuffered=true",
        ]
        if snapshot_moref: cmd.append(f"snapshot={snapshot_moref}")
        cmd.append(f"file={disk_file}")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 30
            while not sock.exists() and time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise IncrementalUnavailable(f"nbdkit exited: {(proc.stderr.read() if proc.stderr else '').strip()}")
                time.sleep(0.1)
            if not sock.exists(): raise IncrementalUnavailable("nbdkit VDDK did not become ready")
            yield f"nbd+unix:///?socket={sock}"
        finally:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
            shutil.rmtree(root, ignore_errors=True)

    def _read_delta(self, uri: str, extents: list[dict[str, int]], map_path: Path, data_path: Path) -> None:
        map_path.write_text(json.dumps(extents, separators=(",", ":")), encoding="utf-8")
        script = """import json,os\nextents=json.load(open(os.environ['IMV_MAP']))\nchunk=int(os.environ['IMV_CHUNK'])\nwith open(os.environ['IMV_DATA'],'wb') as out:\n for e in extents:\n  p=int(e['start']); r=int(e['length'])\n  while r:\n   n=min(chunk,r); d=h.pread(n,p)\n   if len(d)!=n: raise RuntimeError('short VDDK read')\n   out.write(d); p+=n; r-=n\n"""
        env = {**os.environ, "IMV_MAP": str(map_path), "IMV_DATA": str(data_path), "IMV_CHUNK": str(max(SECTOR, int(self.cfg.options.get("vddk_io_chunk_bytes", 4 * 1024 * 1024))))}
        r = subprocess.run(["nbdsh", "-u", uri, "-c", "-"], input=script, text=True, capture_output=True, env=env, timeout=self.timeout)
        if r.returncode: raise IncrementalUnavailable(f"VDDK read failed: {r.stderr.strip()}")
        if data_path.stat().st_size != sum(int(x["length"]) for x in extents): raise IncrementalUnavailable("delta byte count mismatch")

    def _write_delta(self, uri: str, extents: list[dict[str, int]], data_path: Path) -> None:
        fd, name = tempfile.mkstemp(prefix="immutavault-map-", suffix=".json"); os.close(fd); Path(name).write_text(json.dumps(extents), encoding="utf-8")
        script = """import json,os\nextents=json.load(open(os.environ['IMV_MAP']))\nchunk=int(os.environ['IMV_CHUNK'])\nwith open(os.environ['IMV_DATA'],'rb') as src:\n for e in extents:\n  p=int(e['start']); r=int(e['length'])\n  while r:\n   n=min(chunk,r); d=src.read(n)\n   if len(d)!=n: raise RuntimeError('short delta file')\n   h.pwrite(d,p); p+=n; r-=n\n if src.read(1): raise RuntimeError('trailing delta bytes')\ntry: h.flush()\nexcept Exception: pass\n"""
        try:
            env = {**os.environ, "IMV_MAP": name, "IMV_DATA": str(data_path), "IMV_CHUNK": str(max(SECTOR, int(self.cfg.options.get("vddk_io_chunk_bytes", 4 * 1024 * 1024))))}
            r = subprocess.run(["nbdsh", "-u", uri, "-c", "-"], input=script, text=True, capture_output=True, env=env, timeout=self.timeout)
            if r.returncode: raise RuntimeError(f"VDDK write failed: {r.stderr.strip()}")
        finally:
            Path(name).unlink(missing_ok=True)

    def _delta(self, vm: VM, destination: Path, state: dict[str, Any]) -> Path:
        out = destination / safe_component(vm.name); out.mkdir(parents=True, exist_ok=True); snapshot = None
        with self._vsphere() as si:
            obj = self._find_vm(si, vm)
            if not bool(getattr(obj.config, "changeTrackingEnabled", False)): raise IncrementalUnavailable("CBT became disabled")
            try:
                snapshot = self._snapshot(obj, f"immutavault-v07-{uuid.uuid4().hex[:12]}")
                disks = self._disks(obj, snapshot); fingerprint = self._fingerprint(obj, disks); self._validate(state, disks, fingerprint)
                old = {int(x["ordinal"]): x for x in state["disks"]}; vm_moref = str(obj._moId); snap_moref = str(snapshot._moId)
                delta_dir = out / "cbt-deltas"; delta_dir.mkdir(parents=True, exist_ok=True); layers = []; total = 0
                for disk in disks:
                    extents = self._query(obj, snapshot, disk, str(old[int(disk["ordinal"])]["change_id"]))
                    mp = delta_dir / f"disk-{disk['ordinal']:03d}.map.json"; dp = delta_dir / f"disk-{disk['ordinal']:03d}.data"
                    with self._nbd(vm_moref, str(disk["file_name"]), snap_moref, True) as uri: self._read_delta(uri, extents, mp, dp)
                    changed = sum(int(x["length"]) for x in extents); total += changed
                    layers.append({**disk, "map_file": str(mp.relative_to(destination)), "data_file": str(dp.relative_to(destination)), "data_sha256": sha256_file(dp), "changed_bytes": changed})
                marker = {
                    "schema": SCHEMA, "transport": "vmware-cbt-vddk", "kind": "delta", "seeded": True,
                    "platform": self.cfg.name, "vm_id": vm.id, "vm_name": vm.name, "created_at": datetime.now(timezone.utc).isoformat(),
                    "parent_snapshot_id": str(state.get("last_snapshot_id") or ""), "baseline_snapshot_id": str(state.get("baseline_snapshot_id") or ""),
                    "config_fingerprint": fingerprint, "disks": disks, "delta_disks": layers, "changed_bytes": total, "restore_mode": "ovf+vddk-delta",
                }
                if not marker["parent_snapshot_id"] or not marker["baseline_snapshot_id"]: raise IncrementalUnavailable("CBT parent/baseline state is missing")
                atomic_json(destination / TRANSPORT_MARKER, marker); return out
            finally:
                if snapshot is not None: self._remove_snapshot(snapshot)

    def restore(self, source: Path, *, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        index_path = find_chain_index(source)
        if index_path is None:
            return super().restore(source, target_name=target_name, options=options, dry_run=dry_run)
        index = read_json(index_path) or {}; layers = list(index.get("layers") or [])
        if not layers:
            return super().restore(source, target_name=target_name, options=options, dry_run=dry_run)
        baseline = next((x for x in layers if x.get("kind") == "baseline"), layers[0]); baseline_source = Path(str(baseline["source"])); deltas = [x for x in layers if x.get("kind") == "delta"]
        if dry_run:
            result = super().restore(baseline_source, target_name=target_name, options=options, dry_run=True); result["incremental_layers"] = [x.get("snapshot_id") for x in deltas]; return result
        problems = self._vddk_problems()
        if problems: raise RuntimeError("incremental restore requires VDDK: " + "; ".join(problems))
        result = super().restore(baseline_source, target_name=target_name, options=options, dry_run=False)
        with self._vsphere() as si:
            target = self._find_vm_name(si, target_name)
            if "poweredOff" not in str(getattr(target.runtime, "powerState", "")): raise RuntimeError("target must remain powered off while applying CBT deltas")
            target_disks = self._disks(target, None); vm_moref = str(target._moId)
            for layer in deltas:
                root = Path(str(layer["source"])); marker = read_json(root / TRANSPORT_MARKER)
                if not marker or marker.get("kind") != "delta": raise RuntimeError("CBT delta metadata missing")
                for delta in marker.get("delta_disks") or []:
                    ordinal = int(delta["ordinal"]); disk = target_disks[ordinal]
                    mp = root / str(delta["map_file"]); dp = root / str(delta["data_file"])
                    if sha256_file(dp) != str(delta["data_sha256"]): raise RuntimeError(f"CBT delta SHA-256 mismatch for disk {ordinal}")
                    extents = json.loads(mp.read_text(encoding="utf-8"))
                    with self._nbd(vm_moref, str(disk["file_name"]), None, False) as uri: self._write_delta(uri, extents, dp)
        result["transport"] = "vmware-cbt-vddk"; result["incremental_layers_applied"] = len(deltas); return result
