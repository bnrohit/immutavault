from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml

from .dr_config import DRConfig, parse_dr


@dataclass(frozen=True)
class RetentionConfig:
    keep_within_days: int = 30
    keep_daily: int = 30
    keep_weekly: int = 8
    keep_monthly: int = 12
    keep_yearly: int = 3
    min_restore_points: int = 2


@dataclass(frozen=True)
class RepositoryConfig:
    url: str
    local_path: str
    staging_path: str
    retention_days: int = 30  # backwards-compatible alias for keep_within_days
    verify_percent: int = 5
    cacert: str | None = None
    lock_file: str = "/run/lock/immutavault.lock"
    retention: RetentionConfig = field(default_factory=RetentionConfig)


@dataclass(frozen=True)
class RuntimeConfig:
    state_db: str = "/var/lib/immutavault/state.db"
    command_timeout_seconds: int = 14400
    keep_staging_on_failure: bool = True
    restore_staging_path: str = "/srv/immutavault/restore-staging"
    verify_staging_path: str = "/srv/immutavault/verify-staging"
    staging_min_free_percent: int = 10
    state_backup_path: str = "/var/lib/immutavault/state-backups"
    state_backup_keep: int = 288


@dataclass(frozen=True)
class PlatformConfig:
    name: str
    type: str
    endpoint: str
    enabled: bool = True
    mode: str = "export"
    ssh_user: str | None = None
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtectionConfig:
    rpo_target_minutes: int = 1440
    anomaly_data_added_ratio: float = 0.70
    anomaly_size_change_ratio: float = 0.50
    preserve_suspicious_points_days: int = 90
    verify_after_backup: bool = False
    require_four_eyes_restore: bool = True


@dataclass(frozen=True)
class PortalUserConfig:
    name: str
    role: str
    token_env: str
    sources: list[str] = field(default_factory=lambda: ["*"])
    vm_patterns: list[str] = field(default_factory=lambda: ["*"])


@dataclass(frozen=True)
class PortalConfig:
    enabled: bool = True
    listen: str = "127.0.0.1"
    port: int = 8787
    tls_cert: str | None = None
    tls_key: str | None = None
    users: list[PortalUserConfig] = field(default_factory=list)


@dataclass(frozen=True)
class ReplicaConfig:
    name: str
    backend: str = "rest"  # rest, filesystem, s3
    url: str | None = None
    path: str | None = None
    enabled: bool = True
    cacert: str | None = None
    password_env: str = "RESTIC_REPLICA_PASSWORD"
    provider: str = "custom"
    endpoint: str | None = None
    bucket: str | None = None
    prefix: str = "immutavault"
    region: str | None = None
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    session_token_env: str | None = None
    bucket_lookup: str = "path"
    connections: int = 10
    mount_required: bool = False
    mount_type: str | None = None
    mount_source: str | None = None
    object_lock_enabled: bool = False
    object_lock_mode: str = "COMPLIANCE"
    object_lock_days: int = 30
    r2_bucket_lock_enabled: bool = False
    r2_bucket_lock_days: int = 30
    r2_lock_rule_id: str = "immutavault-retention"
    r2_account_id_env: str = "CLOUDFLARE_ACCOUNT_ID"
    r2_api_token_env: str = "CLOUDFLARE_API_TOKEN"


@dataclass(frozen=True)
class Config:
    repository: RepositoryConfig
    runtime: RuntimeConfig
    platforms: list[PlatformConfig]
    protection: ProtectionConfig = field(default_factory=ProtectionConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    replicas: list[ReplicaConfig] = field(default_factory=list)
    dr: DRConfig = field(default_factory=DRConfig)


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ValueError(f"missing required config key: {key}")
    return mapping[key]


def _positive_int(value: Any, name: str, *, minimum: int = 0) -> int:
    value = int(value)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    repo_raw = raw.get("repository") or {}
    runtime_raw = raw.get("runtime") or {}
    protection_raw = raw.get("protection") or {}
    portal_raw = raw.get("portal") or {}
    platforms_raw = raw.get("platforms") or []
    replicas_raw = raw.get("replicas") or []
    dr_raw = raw.get("disaster_recovery") or {}

    retention_raw = repo_raw.get("retention") or {}
    retention_days = int(repo_raw.get("retention_days", retention_raw.get("keep_within_days", 30)))
    if retention_days < 1:
        raise ValueError("repository.retention_days must be >= 1")

    verify_percent = int(repo_raw.get("verify_percent", 5))
    if not 0 <= verify_percent <= 100:
        raise ValueError("repository.verify_percent must be between 0 and 100")

    retention = RetentionConfig(
        keep_within_days=_positive_int(retention_raw.get("keep_within_days", retention_days), "keep_within_days", minimum=1),
        keep_daily=_positive_int(retention_raw.get("keep_daily", 30), "keep_daily"),
        keep_weekly=_positive_int(retention_raw.get("keep_weekly", 8), "keep_weekly"),
        keep_monthly=_positive_int(retention_raw.get("keep_monthly", 12), "keep_monthly"),
        keep_yearly=_positive_int(retention_raw.get("keep_yearly", 3), "keep_yearly"),
        min_restore_points=_positive_int(retention_raw.get("min_restore_points", 2), "min_restore_points", minimum=1),
    )

    repository = RepositoryConfig(
        url=str(_require(repo_raw, "url")),
        local_path=str(_require(repo_raw, "local_path")),
        staging_path=str(_require(repo_raw, "staging_path")),
        retention_days=retention.keep_within_days,
        verify_percent=verify_percent,
        cacert=repo_raw.get("cacert"),
        lock_file=str(repo_raw.get("lock_file", "/run/lock/immutavault.lock")),
        retention=retention,
    )

    free_pct = int(runtime_raw.get("staging_min_free_percent", 10))
    if not 0 <= free_pct <= 95:
        raise ValueError("runtime.staging_min_free_percent must be between 0 and 95")
    command_timeout = int(runtime_raw.get("command_timeout_seconds", 14400))
    if command_timeout < 30:
        raise ValueError("runtime.command_timeout_seconds must be >= 30")
    runtime = RuntimeConfig(
        state_db=str(runtime_raw.get("state_db", "/var/lib/immutavault/state.db")),
        command_timeout_seconds=command_timeout,
        keep_staging_on_failure=bool(runtime_raw.get("keep_staging_on_failure", True)),
        restore_staging_path=str(runtime_raw.get("restore_staging_path", "/srv/immutavault/restore-staging")),
        verify_staging_path=str(runtime_raw.get("verify_staging_path", "/srv/immutavault/verify-staging")),
        staging_min_free_percent=free_pct,
        state_backup_path=str(runtime_raw.get("state_backup_path", "/var/lib/immutavault/state-backups")),
        state_backup_keep=_positive_int(runtime_raw.get("state_backup_keep", 288), "runtime.state_backup_keep", minimum=1),
    )

    anomaly_added = float(protection_raw.get("anomaly_data_added_ratio", 0.70))
    anomaly_size = float(protection_raw.get("anomaly_size_change_ratio", 0.50))
    if not 0.0 <= anomaly_added <= 10.0:
        raise ValueError("protection.anomaly_data_added_ratio must be between 0 and 10")
    if not 0.0 <= anomaly_size <= 10.0:
        raise ValueError("protection.anomaly_size_change_ratio must be between 0 and 10")
    preserve_days = int(protection_raw.get("preserve_suspicious_points_days", 90))
    if preserve_days < retention.keep_within_days:
        raise ValueError("protection.preserve_suspicious_points_days must be >= repository retention keep_within_days")
    rpo_target = int(protection_raw.get("rpo_target_minutes", 1440))
    if not 1 <= rpo_target <= 10080:
        raise ValueError("protection.rpo_target_minutes must be between 1 and 10080")
    protection = ProtectionConfig(
        rpo_target_minutes=rpo_target,
        anomaly_data_added_ratio=anomaly_added,
        anomaly_size_change_ratio=anomaly_size,
        preserve_suspicious_points_days=preserve_days,
        verify_after_backup=bool(protection_raw.get("verify_after_backup", False)),
        require_four_eyes_restore=bool(protection_raw.get("require_four_eyes_restore", True)),
    )

    users: list[PortalUserConfig] = []
    allowed_roles = {"viewer", "restore_operator", "approver", "admin"}
    for item in portal_raw.get("users", []) or []:
        role = str(item.get("role", "viewer")).lower()
        if role not in allowed_roles:
            raise ValueError(f"unsupported portal role: {role}")
        users.append(PortalUserConfig(
            name=str(_require(item, "name")),
            role=role,
            token_env=str(_require(item, "token_env")),
            sources=list(item.get("sources", ["*"])),
            vm_patterns=list(item.get("vm_patterns", ["*"])),
        ))
    portal_port = int(portal_raw.get("port", 8787))
    if not 1 <= portal_port <= 65535:
        raise ValueError("portal.port must be between 1 and 65535")
    portal = PortalConfig(
        enabled=bool(portal_raw.get("enabled", True)),
        listen=str(portal_raw.get("listen", "127.0.0.1")),
        port=portal_port,
        tls_cert=portal_raw.get("tls_cert"),
        tls_key=portal_raw.get("tls_key"),
        users=users,
    )

    platforms: list[PlatformConfig] = []
    allowed_types = {"vmware", "proxmox", "xcpng"}
    for item in platforms_raw:
        ptype = str(_require(item, "type")).lower()
        if ptype not in allowed_types:
            raise ValueError(f"unsupported platform type: {ptype}")
        platforms.append(PlatformConfig(
            name=str(_require(item, "name")),
            type=ptype,
            endpoint=str(_require(item, "endpoint")),
            enabled=bool(item.get("enabled", True)),
            mode=str(item.get("mode", "export")),
            ssh_user=item.get("ssh_user"),
            include=list(item.get("include", ["*"])),
            exclude=list(item.get("exclude", [])),
            options=dict(item.get("options", {})),
        ))

    platform_names = [p.name for p in platforms]
    if len(platform_names) != len(set(platform_names)):
        raise ValueError("platform names must be unique")

    replicas: list[ReplicaConfig] = []
    allowed_backends = {"rest", "filesystem", "s3"}
    allowed_s3_providers = {"custom", "aws", "wasabi", "idrive_e2", "backblaze_b2", "cloudflare_r2", "minio", "ceph"}
    for item in replicas_raw:
        backend = str(item.get("backend", "rest")).lower()
        if backend not in allowed_backends:
            raise ValueError(f"unsupported replica backend: {backend}")
        provider = str(item.get("provider", "custom")).lower()
        if provider not in allowed_s3_providers:
            raise ValueError(f"unsupported S3 provider preset: {provider}")
        url = item.get("url")
        path_value = item.get("path")
        endpoint = item.get("endpoint")
        bucket = item.get("bucket")
        if backend == "rest" and not url:
            raise ValueError(f"replica {item.get('name','?')}: url is required for rest backend")
        if backend == "filesystem" and not path_value:
            raise ValueError(f"replica {item.get('name','?')}: path is required for filesystem backend")
        if backend == "s3" and (not endpoint or not bucket):
            raise ValueError(f"replica {item.get('name','?')}: endpoint and bucket are required for s3 backend")
        object_lock_enabled = bool(item.get("object_lock_enabled", False))
        if object_lock_enabled and provider == "cloudflare_r2":
            raise ValueError("Cloudflare R2 does not expose S3 Object Lock; use r2_bucket_lock_enabled for Cloudflare-native Bucket Locks")
        r2_bucket_lock_enabled = bool(item.get("r2_bucket_lock_enabled", False))
        if r2_bucket_lock_enabled and provider != "cloudflare_r2":
            raise ValueError("r2_bucket_lock_enabled is valid only for provider=cloudflare_r2")
        mode = str(item.get("object_lock_mode", "COMPLIANCE")).upper()
        if mode not in {"COMPLIANCE", "GOVERNANCE"}:
            raise ValueError("object_lock_mode must be COMPLIANCE or GOVERNANCE")
        object_lock_days = _positive_int(item.get("object_lock_days", retention.keep_within_days), "object_lock_days", minimum=1)
        r2_bucket_lock_days = _positive_int(item.get("r2_bucket_lock_days", retention.keep_within_days), "r2_bucket_lock_days", minimum=1)
        bucket_lookup = str(item.get("bucket_lookup", "path")).lower()
        if bucket_lookup not in {"auto", "dns", "path"}:
            raise ValueError("bucket_lookup must be auto, dns, or path")
        replicas.append(ReplicaConfig(
            name=str(_require(item, "name")),
            backend=backend,
            url=str(url) if url else None,
            path=str(path_value) if path_value else None,
            enabled=bool(item.get("enabled", True)),
            cacert=item.get("cacert"),
            password_env=str(item.get("password_env", "RESTIC_REPLICA_PASSWORD")),
            provider=provider,
            endpoint=str(endpoint) if endpoint else None,
            bucket=str(bucket) if bucket else None,
            prefix=str(item.get("prefix", "immutavault")).strip("/"),
            region=str(item.get("region")) if item.get("region") else None,
            access_key_env=str(item.get("access_key_env", "AWS_ACCESS_KEY_ID")),
            secret_key_env=str(item.get("secret_key_env", "AWS_SECRET_ACCESS_KEY")),
            session_token_env=str(item.get("session_token_env")) if item.get("session_token_env") else None,
            bucket_lookup=bucket_lookup,
            connections=_positive_int(item.get("connections", 10), "connections", minimum=1),
            mount_required=bool(item.get("mount_required", False)),
            mount_type=str(item.get("mount_type")) if item.get("mount_type") else None,
            mount_source=str(item.get("mount_source")) if item.get("mount_source") else None,
            object_lock_enabled=object_lock_enabled,
            object_lock_mode=mode,
            object_lock_days=object_lock_days,
            r2_bucket_lock_enabled=r2_bucket_lock_enabled,
            r2_bucket_lock_days=r2_bucket_lock_days,
            r2_lock_rule_id=str(item.get("r2_lock_rule_id", "immutavault-retention")),
            r2_account_id_env=str(item.get("r2_account_id_env", "CLOUDFLARE_ACCOUNT_ID")),
            r2_api_token_env=str(item.get("r2_api_token_env", "CLOUDFLARE_API_TOKEN")),
        ))

    replica_names = [r.name for r in replicas]
    if len(replica_names) != len(set(replica_names)):
        raise ValueError("replica names must be unique")

    dr = parse_dr(dr_raw)
    if dr.enabled:
        by_platform = {p.name: p for p in platforms}
        by_replica = {r.name: r for r in replicas}
        dr_replica = by_replica.get(dr.replica)
        if not dr_replica or not dr_replica.enabled:
            raise ValueError(f"DR replica {dr.replica!r} must reference an enabled configured replica")
        for workload in dr.workloads:
            src = by_platform.get(workload.source_platform)
            dst = by_platform.get(workload.target_platform)
            if not src or not src.enabled:
                raise ValueError(f"DR workload {workload.name}: source platform {workload.source_platform!r} is missing or disabled")
            if not dst or not dst.enabled:
                raise ValueError(f"DR workload {workload.name}: target platform {workload.target_platform!r} is missing or disabled")

    return Config(
        repository=repository,
        runtime=runtime,
        platforms=platforms,
        protection=protection,
        portal=portal,
        replicas=replicas,
        dr=dr,
    )
