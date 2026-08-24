from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen
from typing import Any

from .config import ReplicaConfig
from .runner import run


OBJECT_LOCK_CAPABLE = {"aws", "wasabi", "idrive_e2", "backblaze_b2", "custom", "minio", "ceph"}
OBJECT_LOCK_UNSUPPORTED = {"cloudflare_r2"}


@dataclass(frozen=True)
class TargetHealth:
    ok: bool
    problems: list[str]
    details: dict[str, Any]


def restic_target_url(cfg: ReplicaConfig) -> str:
    if cfg.backend == "rest":
        if not cfg.url:
            raise ValueError(f"replica {cfg.name}: missing REST repository url")
        return cfg.url
    if cfg.backend == "filesystem":
        if not cfg.path:
            raise ValueError(f"replica {cfg.name}: missing filesystem path")
        return cfg.path
    if cfg.backend == "s3":
        if not cfg.endpoint or not cfg.bucket:
            raise ValueError(f"replica {cfg.name}: missing S3 endpoint/bucket")
        endpoint = cfg.endpoint.rstrip("/")
        prefix = f"/{cfg.prefix.strip('/')}" if cfg.prefix else ""
        return f"s3:{endpoint}/{cfg.bucket}{prefix}"
    raise ValueError(f"unsupported replica backend: {cfg.backend}")


def target_env(cfg: ReplicaConfig) -> dict[str, str]:
    password = os.getenv(cfg.password_env)
    if not password:
        raise RuntimeError(f"{cfg.password_env} is not set for replica {cfg.name}")
    env = {"RESTIC_PASSWORD": password, "RESTIC_REPOSITORY": restic_target_url(cfg)}
    if cfg.cacert:
        env["RESTIC_CACERT"] = cfg.cacert
    if cfg.backend == "s3":
        key = os.getenv(cfg.access_key_env)
        secret = os.getenv(cfg.secret_key_env)
        if not key or not secret:
            raise RuntimeError(
                f"S3 credentials are missing for {cfg.name}: set {cfg.access_key_env} and {cfg.secret_key_env}"
            )
        env["AWS_ACCESS_KEY_ID"] = key
        env["AWS_SECRET_ACCESS_KEY"] = secret
        if cfg.session_token_env and os.getenv(cfg.session_token_env):
            env["AWS_SESSION_TOKEN"] = os.environ[cfg.session_token_env]
        if cfg.region:
            env["AWS_DEFAULT_REGION"] = cfg.region
    return env


def restic_options(cfg: ReplicaConfig) -> list[str]:
    if cfg.backend != "s3":
        return []
    opts = ["-o", f"s3.bucket-lookup={cfg.bucket_lookup}", "-o", f"s3.connections={cfg.connections}"]
    if cfg.region:
        opts += ["-o", f"s3.region={cfg.region}"]
    return opts


def _mount_info(path: str) -> tuple[bool, str | None, str | None]:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    result = run(["findmnt", "-J", "-T", str(p), "-o", "TARGET,SOURCE,FSTYPE"], timeout=20, check=False)
    if result.returncode != 0:
        return False, None, None
    import json
    try:
        rows = json.loads(result.stdout or "{}").get("filesystems", [])
        if not rows:
            return False, None, None
        row = rows[0]
        return True, row.get("source"), row.get("fstype")
    except Exception:
        return False, None, None


def target_health(cfg: ReplicaConfig) -> TargetHealth:
    problems: list[str] = []
    details: dict[str, Any] = {"name": cfg.name, "backend": cfg.backend, "repository": restic_target_url(cfg)}
    try:
        env = target_env(cfg)
    except Exception as exc:
        return TargetHealth(False, [str(exc)], details)

    if cfg.backend == "filesystem":
        path = Path(cfg.path or "")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"filesystem path unavailable: {exc}")
        mounted, source, fstype = _mount_info(str(path))
        details.update({"mounted": mounted, "mount_source": source, "mount_type": fstype})
        if cfg.mount_required and not mounted:
            problems.append("filesystem target is configured mount_required=true but is not a separate mounted filesystem")
        if cfg.mount_type and fstype and cfg.mount_type.lower() not in fstype.lower():
            problems.append(f"mounted filesystem type is {fstype}, expected {cfg.mount_type}")
        if cfg.mount_source and source and cfg.mount_source not in source:
            problems.append(f"mounted source is {source}, expected to contain {cfg.mount_source}")

    if shutil.which("restic"):
        result = run(["restic", *restic_options(cfg), "snapshots", "--json"], timeout=90, env=env, check=False)
        details["restic_repository_reachable"] = result.returncode == 0
        if result.returncode != 0:
            problems.append("restic repository is not initialized or not reachable")
    else:
        problems.append("restic is not installed")

    if cfg.provider == "cloudflare_r2" and cfg.r2_bucket_lock_enabled:
        if os.getenv(cfg.r2_account_id_env) and os.getenv(cfg.r2_api_token_env):
            try:
                lock = r2_bucket_lock_status(cfg)
                details["provider_lock"] = lock
                if not lock.get("enabled"):
                    problems.append("Cloudflare R2 Bucket Lock rules are incomplete or disabled")
            except Exception as exc:
                problems.append(f"Cloudflare R2 Bucket Lock verification failed: {exc}")
        else:
            details["provider_lock"] = {
                "configured": True,
                "verified": False,
                "note": "admin Cloudflare credentials are not loaded in this service environment",
            }
    return TargetHealth(not problems, problems, details)


def init_target(cfg: ReplicaConfig) -> dict[str, Any]:
    env = target_env(cfg)
    if cfg.backend == "filesystem":
        Path(cfg.path or "").mkdir(parents=True, exist_ok=True)
    probe = run(["restic", *restic_options(cfg), "snapshots", "--json"], timeout=90, env=env, check=False)
    if probe.returncode != 0:
        run(["restic", *restic_options(cfg), "init"], timeout=180, env=env)
        created = True
    else:
        created = False
    return {"name": cfg.name, "repository": restic_target_url(cfg), "created": created}


def _s3_client(cfg: ReplicaConfig):
    if cfg.backend != "s3":
        raise ValueError("S3 client requested for a non-S3 replica")
    if cfg.provider in OBJECT_LOCK_UNSUPPORTED:
        raise RuntimeError(f"{cfg.provider} does not support S3 Object Lock")
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:  # pragma: no cover - installation error path
        raise RuntimeError("boto3 is required for S3 Object Lock support") from exc
    key = os.getenv(cfg.access_key_env)
    secret = os.getenv(cfg.secret_key_env)
    token = os.getenv(cfg.session_token_env) if cfg.session_token_env else None
    if not key or not secret:
        raise RuntimeError(f"missing S3 credentials for {cfg.name}")
    addressing = "virtual" if cfg.bucket_lookup == "dns" else "path"
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        region_name=cfg.region or "us-east-1",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        aws_session_token=token,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": addressing}),
    )


def s3_preflight(cfg: ReplicaConfig) -> dict[str, Any]:
    if cfg.backend != "s3":
        raise ValueError("S3 preflight requires backend=s3")
    client = _s3_client(cfg)
    client.head_bucket(Bucket=cfg.bucket)
    result: dict[str, Any] = {
        "name": cfg.name,
        "provider": cfg.provider,
        "bucket": cfg.bucket,
        "endpoint": cfg.endpoint,
        "object_lock_requested": cfg.object_lock_enabled,
    }
    if cfg.object_lock_enabled:
        try:
            lock = client.get_object_lock_configuration(Bucket=cfg.bucket)
            enabled = lock.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"
        except Exception as exc:
            raise RuntimeError(f"cannot confirm Object Lock on bucket {cfg.bucket}: {exc}") from exc
        if not enabled:
            raise RuntimeError(f"S3 Object Lock is not enabled on bucket {cfg.bucket}")
        result["object_lock_enabled"] = True
    return result


def apply_object_lock(cfg: ReplicaConfig, *, minimum_days: int | None = None) -> dict[str, Any]:
    """Lock all persistent restic repository objects except transient restic lock files.

    This deliberately does not use bucket-default retention because restic needs to create
    and delete its own `locks/` objects. Object retention is applied after a successful copy.
    Existing longer retention is never shortened.
    """
    if not cfg.object_lock_enabled:
        return {"enabled": False, "locked": 0, "skipped": 0}
    if cfg.backend != "s3":
        raise ValueError("provider Object Lock is only available for S3 replicas")
    client = _s3_client(cfg)
    s3_preflight(cfg)
    days = max(cfg.object_lock_days, minimum_days or 0)
    retain_until = datetime.now(timezone.utc) + timedelta(days=days)
    base_prefix = cfg.prefix.strip("/")
    list_prefix = f"{base_prefix}/" if base_prefix else ""
    transient_prefix = f"{list_prefix}locks/"
    locked = 0
    skipped = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            key = str(obj.get("Key", ""))
            if not key or key.startswith(transient_prefix):
                skipped += 1
                continue
            existing_until: datetime | None = None
            try:
                current = client.get_object_retention(Bucket=cfg.bucket, Key=key).get("Retention", {})
                existing_until = current.get("RetainUntilDate")
                if existing_until and existing_until.tzinfo is None:
                    existing_until = existing_until.replace(tzinfo=timezone.utc)
            except Exception:
                existing_until = None
            if existing_until and existing_until >= retain_until:
                skipped += 1
                continue
            client.put_object_retention(
                Bucket=cfg.bucket,
                Key=key,
                Retention={"Mode": cfg.object_lock_mode, "RetainUntilDate": retain_until},
            )
            locked += 1
    return {
        "enabled": True,
        "mode": cfg.object_lock_mode,
        "days": days,
        "retain_until": retain_until.isoformat(),
        "locked": locked,
        "skipped": skipped,
    }


def _r2_api(cfg: ReplicaConfig, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if cfg.provider != "cloudflare_r2":
        raise ValueError("Cloudflare R2 API requested for a non-R2 target")
    account_id = os.getenv(cfg.r2_account_id_env)
    token = os.getenv(cfg.r2_api_token_env)
    if not account_id or not token:
        raise RuntimeError(
            f"Cloudflare R2 rolling retention requires {cfg.r2_account_id_env} and {cfg.r2_api_token_env}; "
            "store a dedicated bucket-configuration token in the protected controller environment because "
            "the Date horizon must be refreshed after successful replica copies"
        )
    if not cfg.bucket:
        raise ValueError("R2 bucket is missing")
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{urlquote(account_id, safe='')}/r2/buckets/{urlquote(cfg.bucket, safe='')}/lock"
    )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cloudflare R2 lock API returned HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cloudflare R2 lock API is unreachable: {exc}") from exc
    if data.get("success") is False:
        raise RuntimeError(f"Cloudflare R2 lock API failed: {data.get('errors')}")
    result = data.get("result", data)
    return result if isinstance(result, dict) else {"result": result}


def _r2_date(condition: dict[str, Any]) -> datetime | None:
    value = str(condition.get("date", "")).strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _r2_persistent_lock_specs(cfg: ReplicaConfig, retain_until: datetime) -> list[dict[str, Any]]:
    """Return rolling Cloudflare Bucket Lock rules for persistent restic namespaces.

    A Date horizon is intentional. Restic deduplicates data packs across snapshots, so a
    newly-created snapshot may reference an object that is much older than that snapshot.
    An Age rule based only on object creation time can therefore expire while a new recovery
    point still depends on the object. Refreshing a Date horizon after every successful copy
    keeps all persistent objects protected through the newest recovery point's immutability
    window. The transient `locks/` namespace remains excluded so restic can release locks.
    """
    root = cfg.prefix.strip("/")
    root = f"{root}/" if root else ""
    namespaces = ("data/", "index/", "snapshots/", "keys/", "config")
    suffixes = ("data", "index", "snapshots", "keys", "config")
    date = retain_until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        {
            "id": f"{cfg.r2_lock_rule_id}-{suffix}",
            "enabled": True,
            "prefix": f"{root}{namespace}",
            "condition": {"type": "Date", "date": date},
        }
        for namespace, suffix in zip(namespaces, suffixes)
    ]


def _r2_rule_covers_until(rule: dict[str, Any], desired_until: datetime) -> bool:
    if not rule.get("enabled", True):
        return False
    condition = rule.get("condition") or {}
    ctype = condition.get("type")
    if ctype == "Indefinite":
        return True
    if ctype != "Date":
        # Age rules are not sufficient for a deduplicated rolling repository: old packs can
        # age out while a new snapshot still references them.
        return False
    existing = _r2_date(condition)
    return bool(existing and existing >= desired_until)


def r2_bucket_lock_status(cfg: ReplicaConfig) -> dict[str, Any]:
    """Read Cloudflare-native Bucket Lock rules for an R2 target.

    R2 Bucket Locks are provider-native retention controls, not S3 Object Lock. For a daily
    protection schedule, permit up to one day of elapsed horizon between refreshes while
    still requiring every persistent namespace to be protected into the future.
    """
    if cfg.provider != "cloudflare_r2":
        raise ValueError("R2 bucket-lock status requires provider=cloudflare_r2")
    result = _r2_api(cfg, "GET")
    rules = result.get("rules", []) or []
    now = datetime.now(timezone.utc)
    desired_until = now + timedelta(days=max(0, cfg.r2_bucket_lock_days - 1))
    expected = _r2_persistent_lock_specs(cfg, desired_until)
    by_id = {str(r.get("id")): r for r in rules}
    matched = [by_id.get(rule["id"]) for rule in expected]
    enabled = all(
        actual
        and str(actual.get("prefix", "")) == wanted["prefix"]
        and _r2_rule_covers_until(actual, desired_until)
        for wanted, actual in zip(expected, matched)
    )
    expiry_dates = [
        _r2_date((rule or {}).get("condition") or {})
        for rule in matched
        if rule and (rule.get("condition") or {}).get("type") == "Date"
    ]
    finite_dates = [d for d in expiry_dates if d]
    return {
        "enabled": bool(enabled),
        "kind": "cloudflare_r2_bucket_lock",
        "strength": "provider_bucket_policy_admin_mutable",
        "rule_id_base": cfg.r2_lock_rule_id,
        "protected_prefixes": [rule["prefix"] for rule in expected],
        "transient_prefix_excluded": f"{cfg.prefix.strip('/') + '/' if cfg.prefix.strip('/') else ''}locks/",
        "minimum_retain_until": min(finite_dates).isoformat() if finite_dates else None,
        "matched_rules": [rule for rule in matched if rule],
        "rules": rules,
    }


def ensure_r2_bucket_lock(cfg: ReplicaConfig, *, minimum_days: int | None = None) -> dict[str, Any]:
    """Create or strengthen Immutavault Cloudflare R2 Bucket Lock rules.

    Persistent restic namespaces are locked separately while `locks/` remains excluded.
    Unrelated rules are preserved. Existing indefinite or later Date rules are never shortened;
    legacy Age rules are upgraded to a rolling Date horizon for deduplicated-repository safety.
    This requires a protected Cloudflare bucket-configuration admin token.
    """
    if cfg.provider != "cloudflare_r2" or not cfg.r2_bucket_lock_enabled:
        raise ValueError("Cloudflare R2 Bucket Lock is not enabled for this target")
    result = _r2_api(cfg, "GET")
    rules = list(result.get("rules", []) or [])
    days = max(cfg.r2_bucket_lock_days, minimum_days or 0)
    retain_until = datetime.now(timezone.utc) + timedelta(days=days)
    desired = _r2_persistent_lock_specs(cfg, retain_until)
    root = cfg.prefix.strip("/")
    root_prefix = f"{root}/" if root else ""

    # Remove an unreleased-development legacy broad rule if present because it would also
    # cover restic's transient `locks/` objects. Never remove unrelated administrator rules.
    rules = [
        r for r in rules
        if not (str(r.get("id")) == cfg.r2_lock_rule_id and str(r.get("prefix", "")) == root_prefix)
    ]
    current_by_id = {str(r.get("id")): (idx, r) for idx, r in enumerate(rules)}
    changed = False
    effective: list[dict[str, Any]] = []
    for wanted in desired:
        found = current_by_id.get(wanted["id"])
        if found is None:
            rules.append(wanted)
            effective.append(wanted)
            changed = True
            continue
        idx, existing = found
        if _r2_rule_covers_until(existing, retain_until) and str(existing.get("prefix", "")) == wanted["prefix"]:
            effective.append(existing)
            continue
        rules[idx] = wanted
        effective.append(wanted)
        changed = True

    if changed:
        _r2_api(cfg, "PUT", {"rules": rules})
    return {
        "enabled": True,
        "kind": "cloudflare_r2_bucket_lock",
        "strength": "provider_bucket_policy_admin_mutable",
        "rule_id_base": cfg.r2_lock_rule_id,
        "days": days,
        "retain_until": retain_until.isoformat(),
        "changed": changed,
        "protected_prefixes": [r["prefix"] for r in desired],
        "transient_prefix_excluded": f"{root_prefix}locks/",
        "rules": effective,
    }
