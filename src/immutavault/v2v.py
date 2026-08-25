from __future__ import annotations

from dataclasses import dataclass, asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tarfile
import tempfile
from typing import Any
import xml.etree.ElementTree as ET

from .adapters.proxmox import ProxmoxAdapter
from .config import PlatformConfig
from .runner import run
from .util import safe_component
from .v2v_config import V10Config, V2VProviderConfig


BUILTIN_PAIR = "vmware:proxmox"
V2V_PROTOCOL = 1
BRIDGE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
STORAGE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class V2VPlan:
    allowed: bool
    source_type: str
    target_type: str
    mode: str
    certification_id: str | None = None
    provider: str | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True)
class V2VInspection:
    os_family: str
    distro: str
    osinfo: str
    arch: str
    firmware: str
    disk_count: int
    virtual_bytes: int
    secure_boot: bool
    source_tpm: bool
    source_networks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_networks"] = list(self.source_networks)
        return value


@dataclass(frozen=True)
class ConvertedDisk:
    path: Path
    format: str
    virtual_size: int


@dataclass(frozen=True)
class ConvertedNIC:
    mac: str | None
    source_network: str | None


@dataclass(frozen=True)
class ConvertedBundle:
    domain_xml: Path
    disks: tuple[ConvertedDisk, ...]
    nics: tuple[ConvertedNIC, ...]
    memory_mib: int
    vcpus: int
    firmware: str
    inspection: V2VInspection


class V2VManager:
    def __init__(self, cfg: V10Config) -> None:
        self.cfg = cfg

    def _pair(self, source_type: str, target_type: str) -> str:
        return f"{source_type.lower()}:{target_type.lower()}"

    def _provider_for_pair(self, pair: str) -> V2VProviderConfig | None:
        for provider in self.cfg.v2v.providers:
            if provider.enabled and pair in provider.pairs:
                return provider
        return None

    def plan(self, point: dict[str, Any], target: PlatformConfig, options: dict[str, Any] | None = None) -> V2VPlan:
        source_type = str(point.get("platform_type") or "").lower()
        target_type = target.type.lower()
        if source_type == target_type:
            return V2VPlan(True, source_type, target_type, "native", certification_id="native-family-restore")
        reasons: list[str] = []
        if not self.cfg.v2v.enabled:
            reasons.append("v2v.enabled is false")
        if self.cfg.v2v.require_verified_point and not bool(point.get("verified")):
            reasons.append("recovery point has not passed full verification")
        if bool(point.get("suspicious")) and not self.cfg.v2v.allow_suspicious_points:
            reasons.append("recovery point is marked suspicious")
        pair = self._pair(source_type, target_type)
        provider = self._provider_for_pair(pair)
        if pair == BUILTIN_PAIR and self.cfg.v2v.builtin_vmware_to_proxmox:
            storage = str((options or {}).get("storage") or target.options.get("v2v_storage") or target.options.get("restore_storage") or "").strip()
            if not storage:
                reasons.append("target Proxmox platform requires options.v2v_storage or restore storage")
            elif not STORAGE_RE.fullmatch(storage):
                reasons.append("target V2V storage id contains unsupported characters")
            if reasons:
                return V2VPlan(False, source_type, target_type, "builtin", "immutavault-vmware-proxmox-v1", reasons=tuple(reasons))
            return V2VPlan(True, source_type, target_type, "builtin", "immutavault-vmware-proxmox-v1")
        if provider is not None:
            if reasons:
                return V2VPlan(False, source_type, target_type, "provider", provider.certification_id, provider.name, tuple(reasons))
            return V2VPlan(True, source_type, target_type, "provider", provider.certification_id, provider.name)
        reasons.append(f"no certified conversion provider is configured for {pair}")
        return V2VPlan(False, source_type, target_type, "blocked", reasons=tuple(reasons))

    def doctor(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not self.cfg.v2v.enabled:
            return {"v2v": ["V2V is disabled by policy"]}
        if self.cfg.v2v.builtin_vmware_to_proxmox:
            result[BUILTIN_PAIR] = self._builtin_doctor()
        for provider in self.cfg.v2v.providers:
            if provider.enabled:
                result[f"provider:{provider.name}"] = self._provider_doctor(provider)
        return result

    def _version_tuple(self, text: str) -> tuple[int, int, int]:
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
        if not match:
            return (0, 0, 0)
        return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))

    def _builtin_doctor(self) -> list[str]:
        problems: list[str] = []
        for command in ("virt-v2v", "virt-v2v-inspector", "qemu-img"):
            if shutil.which(command) is None:
                problems.append(f"{command} is not installed")
        if problems:
            return problems
        version = run(["virt-v2v", "--version"], timeout=30, check=False)
        minimum = self._version_tuple(self.cfg.v2v.virt_v2v_min_version)
        actual = self._version_tuple(version.stdout + version.stderr)
        if version.returncode != 0 or actual < minimum:
            problems.append(
                f"virt-v2v {actual[0]}.{actual[1]}.{actual[2]} is below certified minimum {self.cfg.v2v.virt_v2v_min_version}"
            )
        machine = run(["virt-v2v", "--machine-readable"], timeout=30, check=False)
        features = {line.strip() for line in machine.stdout.splitlines() if line.strip()}
        required = {"input:ova", "output:local", "convert:linux", "convert:windows"}
        missing = sorted(required - features)
        if machine.returncode != 0 or missing:
            problems.append("virt-v2v lacks certified capabilities: " + ", ".join(missing or ["machine-readable probe failed"]))
        inspect = run(["virt-v2v-inspector", "--help"], timeout=30, check=False)
        if inspect.returncode != 0:
            problems.append("virt-v2v-inspector capability probe failed")
        image = run(["qemu-img", "--version"], timeout=30, check=False)
        if image.returncode != 0:
            problems.append("qemu-img capability probe failed")
        return problems

    def _sha256_file(self, path: Path) -> str:
        h = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _provider_payload(self, provider: V2VProviderConfig, action: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        helper = Path(provider.helper)
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise RuntimeError(f"V2V provider {provider.name} helper is missing or not executable")
        actual = self._sha256_file(helper)
        if actual != provider.sha256:
            raise RuntimeError(f"V2V provider {provider.name} SHA-256 mismatch; refusing unpinned helper")
        result = run(
            [str(helper), action],
            timeout=self.cfg.v2v.provider_timeout_seconds,
            input_text=json.dumps(request or {}, separators=(",", ":")),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"V2V provider {provider.name} {action} failed: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"V2V provider {provider.name} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"V2V provider {provider.name} returned a non-object response")
        return payload

    def _provider_doctor(self, provider: V2VProviderConfig) -> list[str]:
        try:
            payload = self._provider_payload(provider, "capabilities")
        except Exception as exc:
            return [str(exc)]
        problems: list[str] = []
        if int(payload.get("protocol", 0)) != V2V_PROTOCOL:
            problems.append("provider protocol mismatch")
        if payload.get("certification_id") != provider.certification_id:
            problems.append("provider certification_id does not match configuration")
        pairs = {str(x) for x in payload.get("pairs", [])}
        missing_pairs = sorted(set(provider.pairs) - pairs)
        if missing_pairs:
            problems.append("provider does not advertise configured pairs: " + ", ".join(missing_pairs))
        features = {str(x) for x in payload.get("features", [])}
        required = {"inspect", "convert", "validate", "rollback"}
        missing = sorted(required - features)
        if missing:
            problems.append("provider lacks required safety features: " + ", ".join(missing))
        return problems

    def _ovf_attr(self, element: ET.Element, suffix: str) -> str | None:
        for key, value in element.attrib.items():
            if key == suffix or key.endswith("}" + suffix):
                return value
        return None

    def _prepare_ova(self, source: Path, work: Path) -> tuple[Path, dict[str, Any]]:
        if not source.is_dir():
            raise RuntimeError("VMware V2V source must be a restored recovery-point directory")
        for item in source.rglob("*"):
            if item.is_symlink():
                raise RuntimeError(f"VMware V2V source contains a symlink and is rejected: {item}")
        ovfs = sorted(source.rglob("*.ovf"))
        if len(ovfs) != 1:
            raise RuntimeError(f"VMware V2V requires exactly one OVF descriptor; found {len(ovfs)}")
        ovf = ovfs[0]
        try:
            tree = ET.parse(ovf)
        except ET.ParseError as exc:
            raise RuntimeError("VMware OVF descriptor is malformed") from exc
        root = tree.getroot()
        files: list[Path] = [ovf]
        seen = {ovf.name}
        for file_node in root.findall(".//{*}File"):
            href = self._ovf_attr(file_node, "href")
            if not href:
                continue
            relative = Path(href)
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                raise RuntimeError(f"OVF contains unsafe external file reference: {href!r}")
            candidate = ovf.parent / relative
            if not candidate.is_file() or candidate.is_symlink():
                raise RuntimeError(f"OVF referenced file is missing or unsafe: {href!r}")
            if candidate.name not in seen:
                files.append(candidate)
                seen.add(candidate.name)
        vmdks = [item for item in files if item.suffix.lower() == ".vmdk"]
        if not vmdks:
            raise RuntimeError("VMware OVF does not reference a VMDK disk")
        manifests = sorted(ovf.parent.glob("*.mf"))
        for manifest in manifests:
            if manifest.name not in seen and not manifest.is_symlink():
                files.append(manifest)
                seen.add(manifest.name)
        text = ovf.read_text(encoding="utf-8", errors="replace").lower()
        source_networks: list[str] = []
        for network in root.findall(".//{*}Network"):
            name = self._ovf_attr(network, "name")
            if name and name not in source_networks:
                source_networks.append(name)
        flags = {
            "secure_boot": ("secureboot" in text or "secure boot" in text) and ("true" in text or "enabled" in text),
            "source_tpm": "virtualtpm" in text or "vtpm" in text or "trustedplatformmodule" in text,
            "firmware_hint": "uefi" if ("firmware" in text and "efi" in text) else "bios",
            "source_networks": source_networks,
        }
        ova = work / "source.ova"
        with tarfile.open(ova, "w", format=tarfile.USTAR_FORMAT) as archive:
            archive.add(ovf, arcname=ovf.name, recursive=False)
            for item in files:
                if item == ovf:
                    continue
                archive.add(item, arcname=item.name, recursive=False)
        return ova, flags

    def _inspection(self, ova: Path, flags: dict[str, Any]) -> V2VInspection:
        result = run(["virt-v2v-inspector", "-i", "ova", str(ova), "-O", "-"], timeout=self.cfg.runtime.command_timeout_seconds)
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise RuntimeError("virt-v2v-inspector returned malformed XML") from exc
        os_node = root.find("operatingsystem")
        if os_node is None:
            raise RuntimeError("virt-v2v-inspector did not identify an operating system")
        text = lambda tag: (os_node.findtext(tag) or "").strip().lower()
        os_family = text("name")
        arch = text("arch")
        if os_family not in {"linux", "windows"}:
            raise RuntimeError(f"guest OS family {os_family or 'unknown'} is outside the certified V2V matrix")
        if arch not in {"x86_64", "amd64"}:
            raise RuntimeError(f"guest architecture {arch or 'unknown'} is outside the certified V2V matrix")
        firmware_node = root.find("firmware")
        firmware = str(firmware_node.attrib.get("type", "") if firmware_node is not None else "").lower()
        if firmware in {"efi", "uefi"}:
            firmware = "uefi"
        elif firmware in {"bios", "legacy"}:
            firmware = "bios"
        else:
            firmware = str(flags.get("firmware_hint") or "bios")
        disks = root.findall("./disks/disk")
        total = 0
        for disk in disks:
            total += int((disk.findtext("virtual-size") or "0").strip() or 0)
        inspection = V2VInspection(
            os_family=os_family,
            distro=text("distro"),
            osinfo=text("osinfo"),
            arch=arch,
            firmware=firmware,
            disk_count=len(disks),
            virtual_bytes=total,
            secure_boot=bool(flags.get("secure_boot")),
            source_tpm=bool(flags.get("source_tpm")),
            source_networks=tuple(str(x) for x in flags.get("source_networks", [])),
        )
        if inspection.disk_count < 1 or inspection.disk_count > self.cfg.v2v.max_disks:
            raise RuntimeError(f"guest disk count {inspection.disk_count} exceeds certified limit {self.cfg.v2v.max_disks}")
        if inspection.virtual_bytes > self.cfg.v2v.max_virtual_bytes:
            raise RuntimeError("guest virtual disk capacity exceeds configured V2V safety limit")
        if inspection.firmware == "uefi" and not self.cfg.v2v.allow_uefi:
            raise RuntimeError("UEFI V2V is disabled by policy")
        if inspection.secure_boot and not self.cfg.v2v.allow_secure_boot:
            raise RuntimeError("Secure Boot guest is blocked; v1.0 does not migrate source Secure Boot trust state")
        if inspection.source_tpm:
            raise RuntimeError("source virtual TPM detected; v1.0 refuses to fabricate or discard TPM state")
        if inspection.os_family == "windows" and not self._virtio_windows_available():
            raise RuntimeError("Windows V2V requires signed VirtIO drivers; configure VIRTIO_WIN or install /usr/share/virtio-win")
        return inspection

    def _virtio_windows_available(self) -> bool:
        configured = os.getenv("VIRTIO_WIN")
        if configured and Path(configured).exists():
            return True
        return Path("/usr/share/virtio-win").exists() or Path("/usr/share/virtio-win/virtio-win.iso").exists()

    def _memory_mib(self, node: ET.Element | None) -> int:
        if node is None or not (node.text or "").strip():
            return 2048
        value = int((node.text or "0").strip())
        unit = str(node.attrib.get("unit", "KiB")).lower()
        if unit in {"kib", "kb"}:
            return max(128, (value + 1023) // 1024)
        if unit in {"mib", "mb"}:
            return max(128, value)
        if unit in {"gib", "gb"}:
            return max(128, value * 1024)
        if unit in {"b", "bytes"}:
            return max(128, (value + 1024 * 1024 - 1) // (1024 * 1024))
        raise RuntimeError(f"unsupported libvirt memory unit: {unit}")

    def _parse_bundle(self, output: Path, inspection: V2VInspection) -> ConvertedBundle:
        xmls = sorted(output.glob("*.xml"))
        if len(xmls) != 1:
            raise RuntimeError(f"virt-v2v output must contain exactly one libvirt XML descriptor; found {len(xmls)}")
        domain_xml = xmls[0]
        try:
            root = ET.parse(domain_xml).getroot()
        except ET.ParseError as exc:
            raise RuntimeError("virt-v2v output libvirt XML is malformed") from exc
        disks: list[ConvertedDisk] = []
        for disk_node in root.findall("./devices/disk"):
            if disk_node.attrib.get("device") != "disk":
                continue
            source_node = disk_node.find("source")
            if source_node is None:
                continue
            filename = source_node.attrib.get("file")
            if not filename:
                continue
            path = Path(filename)
            if not path.is_absolute():
                path = (domain_xml.parent / path).resolve()
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"virt-v2v output disk is missing or unsafe: {path}")
            info = run(["qemu-img", "info", "--output=json", str(path)], timeout=120)
            try:
                payload = json.loads(info.stdout)
                image_format = str(payload["format"])
                virtual_size = int(payload["virtual-size"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"qemu-img returned invalid metadata for {path}") from exc
            if image_format not in {"qcow2", "raw"}:
                raise RuntimeError(f"unexpected converted disk format {image_format!r}")
            disks.append(ConvertedDisk(path=path, format=image_format, virtual_size=virtual_size))
        if len(disks) != inspection.disk_count:
            raise RuntimeError(f"converted disk count {len(disks)} does not match inspected source count {inspection.disk_count}")
        nics: list[ConvertedNIC] = []
        for nic_node in root.findall("./devices/interface"):
            mac_node = nic_node.find("mac")
            source_node = nic_node.find("source")
            mac = mac_node.attrib.get("address") if mac_node is not None else None
            network = None
            if source_node is not None:
                network = source_node.attrib.get("network") or source_node.attrib.get("bridge")
            nics.append(ConvertedNIC(mac=mac, source_network=network))
        memory = self._memory_mib(root.find("memory"))
        vcpus = int((root.findtext("vcpu") or "2").strip() or 2)
        if not 1 <= vcpus <= 1024:
            raise RuntimeError(f"converted vCPU count {vcpus} is outside safety bounds")
        return ConvertedBundle(
            domain_xml=domain_xml,
            disks=tuple(disks),
            nics=tuple(nics),
            memory_mib=memory,
            vcpus=vcpus,
            firmware=inspection.firmware,
            inspection=inspection,
        )

    def _convert_vmware(self, source: Path, target_name: str, work: Path) -> ConvertedBundle:
        problems = self._builtin_doctor()
        if problems:
            raise RuntimeError("V2V conversion preflight failed: " + "; ".join(problems))
        ova, flags = self._prepare_ova(source, work)
        inspection = self._inspection(ova, flags)
        output = work / "converted"
        output.mkdir(parents=True, exist_ok=True)
        scratch = work / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        env = {"VIRT_V2V_TMPDIR": str(scratch), "LIBGUESTFS_CACHEDIR": str(scratch)}
        command = [
            "virt-v2v", "--machine-readable", "-i", "ova", str(ova),
            "-o", "local", "-os", str(output), "-of", "qcow2", "-oa", "sparse",
            "-on", safe_component(target_name),
        ]
        result = run(command, timeout=self.cfg.runtime.command_timeout_seconds, env=env, check=False)
        if result.returncode != 0:
            raise RuntimeError("virt-v2v conversion failed: " + (result.stderr.strip() or result.stdout.strip())[-4000:])
        return self._parse_bundle(output, inspection)

    def _mapped_bridges(self, bundle: ConvertedBundle, target: PlatformConfig, options: dict[str, Any]) -> list[str]:
        raw = options.get("network_map")
        if raw is None:
            raw = target.options.get("v2v_network_map", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("V2V network_map must be a mapping of source network to Proxmox bridge")
        mapping = {str(k): str(v) for k, v in raw.items()}
        default = str(options.get("default_bridge") or target.options.get("v2v_default_bridge") or "").strip()
        bridges: list[str] = []
        for index, nic in enumerate(bundle.nics):
            key = nic.source_network or "default"
            bridge = mapping.get(key) or mapping.get(str(index)) or mapping.get("*") or default
            if not bridge and self.cfg.v2v.require_network_mapping:
                raise RuntimeError(f"no certified network mapping exists for source NIC {index} ({key})")
            if not bridge:
                bridge = "vmbr0"
            if not BRIDGE_RE.fullmatch(bridge):
                raise RuntimeError(f"unsafe or invalid Proxmox bridge name: {bridge!r}")
            bridges.append(bridge)
        return bridges

    def _ostype(self, inspection: V2VInspection) -> str:
        if inspection.os_family == "linux":
            return "l26"
        osinfo = inspection.osinfo.lower()
        if "win11" in osinfo or "windows11" in osinfo:
            return "win11"
        if "win10" in osinfo or "windows10" in osinfo or "server2025" in osinfo or "server2022" in osinfo or "server2019" in osinfo:
            return "win10"
        if "win8" in osinfo or "windows8" in osinfo or "server2016" in osinfo or "server2012" in osinfo:
            return "win8"
        if "win7" in osinfo or "windows7" in osinfo or "server2008" in osinfo:
            return "win7"
        return "other"

    def _unused_volume(self, text: str) -> tuple[str, str]:
        rows: list[tuple[int, str, str]] = []
        for line in text.splitlines():
            match = re.match(r"^unused(\d+):\s*([^,\s]+)", line.strip())
            if match:
                rows.append((int(match.group(1)), f"unused{match.group(1)}", match.group(2)))
        if not rows:
            raise RuntimeError("Proxmox import completed without an unused disk entry")
        _, key, volume = sorted(rows)[-1]
        return key, volume

    def _restore_proxmox(self, bundle: ConvertedBundle, target: PlatformConfig, target_name: str, options: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        adapter = ProxmoxAdapter(target, self.cfg.runtime.command_timeout_seconds)
        storage = str(options.get("storage") or target.options.get("v2v_storage") or target.options.get("restore_storage") or "").strip()
        if not STORAGE_RE.fullmatch(storage):
            raise RuntimeError("Proxmox V2V target storage is missing or invalid")
        efi_storage = str(options.get("efi_storage") or target.options.get("v2v_efi_storage") or storage).strip()
        if bundle.firmware == "uefi" and not STORAGE_RE.fullmatch(efi_storage):
            raise RuntimeError("UEFI V2V requires a valid Proxmox EFI storage id")
        bridges = self._mapped_bridges(bundle, target, options)
        vmid = str(options.get("vmid") or "").strip()
        if vmid and not vmid.isdigit():
            raise ValueError("V2V Proxmox vmid must be numeric")
        if not vmid:
            if dry_run:
                vmid = "NEXTID"
            else:
                vmid = run(adapter._ssh() + ["pvesh get /cluster/nextid"], timeout=60).stdout.strip()
        if vmid != "NEXTID" and not vmid.isdigit():
            raise RuntimeError("Proxmox did not return a numeric next VMID")
        create_parts = [
            "qm", "create", vmid, "--name", target_name,
            "--memory", str(bundle.memory_mib), "--cores", str(bundle.vcpus),
            "--ostype", self._ostype(bundle.inspection),
        ]
        if bundle.firmware == "uefi":
            create_parts += ["--bios", "ovmf", "--machine", "q35"]
        else:
            create_parts += ["--bios", "seabios"]
        for index, (nic, bridge) in enumerate(zip(bundle.nics, bridges)):
            model = "virtio"
            if nic.mac:
                model += f"={nic.mac}"
            create_parts += [f"--net{index}", f"{model},bridge={bridge}"]
        if dry_run:
            return {
                "platform": target.name,
                "kind": "qemu",
                "vmid": vmid,
                "name": target_name,
                "mode": "certified-v2v",
                "certification_id": "immutavault-vmware-proxmox-v1",
                "inspection": bundle.inspection.to_dict(),
                "create_command": create_parts,
                "disk_count": len(bundle.disks),
                "network_bridges": bridges,
                "power": "off",
            }
        exists = run(adapter._ssh() + [f"qm status {shlex.quote(vmid)}"], timeout=30, check=False)
        if exists.returncode == 0:
            raise RuntimeError(f"target VMID {vmid} already exists; V2V refuses overwrite")
        remote_paths: list[str] = []
        created = False
        try:
            create_cmd = " ".join(shlex.quote(str(part)) for part in create_parts)
            run(adapter._ssh() + [create_cmd], timeout=120)
            created = True
            if bundle.firmware == "uefi":
                efi = f"{efi_storage}:0,efitype=4m,pre-enrolled-keys=0"
                run(adapter._ssh() + [f"qm set {shlex.quote(vmid)} --efidisk0 {shlex.quote(efi)}"], timeout=120)
            for index, disk in enumerate(bundle.disks):
                remote = f"/var/tmp/immutavault-v2v-{safe_component(vmid)}-{index}.{disk.format}"
                remote_paths.append(remote)
                target_host = adapter._ssh()[-1]
                run(adapter._scp() + [str(disk.path), f"{target_host}:{remote}"], timeout=self.cfg.runtime.command_timeout_seconds)
                import_cmd = f"qm importdisk {shlex.quote(vmid)} {shlex.quote(remote)} {shlex.quote(storage)} --format qcow2"
                run(adapter._ssh() + [import_cmd], timeout=self.cfg.runtime.command_timeout_seconds)
                config = run(adapter._ssh() + [f"qm config {shlex.quote(vmid)}"], timeout=60).stdout
                unused_key, volume = self._unused_volume(config)
                # Removing an unused config key does not free the storage volume. Attach
                # the already imported volume explicitly to the VirtIO bus that virt-v2v
                # prepared the guest to boot from.
                run(adapter._ssh() + [f"qm set {shlex.quote(vmid)} --delete {shlex.quote(unused_key)}"], timeout=60)
                run(adapter._ssh() + [f"qm set {shlex.quote(vmid)} --virtio{index} {shlex.quote(volume)}"], timeout=60)
            boot_order = ";".join(f"virtio{i}" for i in range(len(bundle.disks)))
            run(adapter._ssh() + [f"qm set {shlex.quote(vmid)} --boot {shlex.quote('order=' + boot_order)}"], timeout=60)
            final = run(adapter._ssh() + [f"qm config {shlex.quote(vmid)}"], timeout=60).stdout
            for index in range(len(bundle.disks)):
                if not re.search(rf"^virtio{index}:\s*", final, re.M):
                    raise RuntimeError(f"post-import validation failed: virtio{index} is missing")
            for index in range(len(bundle.nics)):
                if not re.search(rf"^net{index}:\s*", final, re.M):
                    raise RuntimeError(f"post-import validation failed: net{index} is missing")
            if bundle.firmware == "uefi" and not re.search(r"^bios:\s*ovmf\s*$", final, re.M):
                raise RuntimeError("post-import validation failed: UEFI/OVMF is not configured")
            return {
                "platform": target.name,
                "kind": "qemu",
                "vmid": vmid,
                "name": target_name,
                "mode": "certified-v2v",
                "certification_id": "immutavault-vmware-proxmox-v1",
                "inspection": bundle.inspection.to_dict(),
                "disk_count": len(bundle.disks),
                "network_bridges": bridges,
                "power": "off",
                "validation": {
                    "source_manifest_verified": True,
                    "guest_converted_by_virt_v2v": True,
                    "target_configuration_verified": True,
                    "automatic_power_on": False,
                },
            }
        except Exception:
            if created:
                run(adapter._ssh() + [f"qm destroy {shlex.quote(vmid)} --purge 1 --destroy-unreferenced-disks 1"], timeout=600, check=False)
            raise
        finally:
            for remote in remote_paths:
                run(adapter._ssh() + [f"rm -f {shlex.quote(remote)}"], timeout=300, check=False)

    def _execute_provider(self, provider: V2VProviderConfig, *, source: Path, point: dict[str, Any], target: PlatformConfig, target_name: str, options: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        problems = self._provider_doctor(provider)
        if problems:
            raise RuntimeError("certified V2V provider preflight failed: " + "; ".join(problems))
        request = {
            "protocol": V2V_PROTOCOL,
            "action": "plan" if dry_run else "convert",
            "certification_id": provider.certification_id,
            "pair": self._pair(str(point["platform_type"]), target.type),
            "source": {
                "path": str(source), "snapshot_id": str(point["snapshot_id"]),
                "platform": str(point["platform"]), "platform_type": str(point["platform_type"]),
                "vm_name": str(point["vm_name"]), "manifest_verified": True,
            },
            "target": {"platform": target.name, "platform_type": target.type, "target_name": target_name},
            "options": options,
            "safety": {"new_vm_only": True, "source_read_only": True, "automatic_overwrite": False},
        }
        payload = self._provider_payload(provider, "plan" if dry_run else "convert", request)
        if payload.get("status") != "success":
            raise RuntimeError(f"certified V2V provider refused operation: {payload.get('error') or payload.get('reason') or 'unspecified'}")
        if payload.get("certification_id") != provider.certification_id:
            raise RuntimeError("certified V2V provider response certification_id mismatch")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("certified V2V provider omitted result object")
        if not dry_run:
            validation = payload.get("validation")
            if not isinstance(validation, dict):
                raise RuntimeError("certified V2V provider omitted validation attestation")
            required = ("source_read_only", "target_new_vm", "network_mapped", "rollback_available")
            if any(validation.get(key) is not True for key in required):
                raise RuntimeError("certified V2V provider returned incomplete safety validation")
            result["validation"] = validation
        result["mode"] = "certified-provider-v2v"
        result["provider"] = provider.name
        result["certification_id"] = provider.certification_id
        return result

    def execute(self, *, source: Path, point: dict[str, Any], target: PlatformConfig, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        plan = self.plan(point, target, options)
        if not plan.allowed:
            raise RuntimeError("cross-hypervisor recovery blocked: " + "; ".join(plan.reasons))
        if plan.mode == "native":
            raise RuntimeError("native same-family restore must use the normal adapter path")
        pair = self._pair(str(point["platform_type"]), target.type)
        if pair == BUILTIN_PAIR and plan.mode == "builtin":
            with tempfile.TemporaryDirectory(prefix="immutavault-v2v-") as tmp:
                work = Path(tmp)
                bundle = self._convert_vmware(source, target_name, work)
                return self._restore_proxmox(bundle, target, target_name, options, dry_run=dry_run)
        provider = self._provider_for_pair(pair)
        if provider is None:
            raise RuntimeError(f"cross-hypervisor recovery blocked: no certified provider for {pair}")
        return self._execute_provider(
            provider, source=source, point=point, target=target, target_name=target_name,
            options=options, dry_run=dry_run,
        )
