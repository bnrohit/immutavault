from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from pathlib import Path
from typing import Any

import yaml

from .config import Config, load_config


ROLE_ORDER = {"viewer": 0, "restore_operator": 1, "approver": 2, "admin": 3}


@dataclass(frozen=True)
class TenantConfig:
    id: str
    name: str
    platforms: list[str] = field(default_factory=lambda: ["*"])


@dataclass(frozen=True)
class OIDCConfig:
    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret_env: str | None = None
    redirect_uri: str = ""
    session_secret_env: str = "IMMUTAVAULT_OIDC_SESSION_SECRET"
    session_minutes: int = 60
    require_mfa: bool = True
    allowed_tenant_ids: list[str] = field(default_factory=list)
    required_acrs: list[str] = field(default_factory=list)
    allow_local_tokens: bool = False
    default_role: str = "viewer"
    default_tenants: list[str] = field(default_factory=list)
    group_role_map: dict[str, str] = field(default_factory=dict)
    group_tenant_map: dict[str, list[str]] = field(default_factory=dict)
    app_role_map: dict[str, str] = field(default_factory=dict)
    app_role_tenant_map: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservabilityConfig:
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    metrics_token_env: str = "IMMUTAVAULT_METRICS_TOKEN"
    websocket_enabled: bool = True
    websocket_listen: str = "127.0.0.1"
    websocket_port: int = 8788
    websocket_poll_seconds: int = 2
    websocket_ticket_ttl_seconds: int = 60
    websocket_allowed_origins: list[str] = field(default_factory=list)
    include_platform_labels: bool = True


@dataclass(frozen=True)
class EnterpriseAccessConfig:
    user_tenants: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class EnterpriseConfig:
    """Transparent wrapper around the v0.8 core Config.

    Existing backup/DR code continues to consume the core attributes while v0.9
    identity, tenancy, metrics and event-streaming policy is kept in an additive
    validated layer. Older configuration files remain valid with enterprise
    features disabled/defaulted.
    """

    core: Config
    tenants: list[TenantConfig]
    oidc: OIDCConfig
    observability: ObservabilityConfig
    access: EnterpriseAccessConfig

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)

    def tenant_for_platform(self, platform_name: str) -> str:
        matches = [
            tenant.id
            for tenant in self.tenants
            if any(fnmatch.fnmatch(platform_name, pattern) for pattern in tenant.platforms)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"platform {platform_name!r} must resolve to exactly one tenant; resolved {matches or 'none'}"
            )
        return matches[0]

    def tenant_name(self, tenant_id: str) -> str:
        for tenant in self.tenants:
            if tenant.id == tenant_id:
                return tenant.name
        return tenant_id

    def tenant_ids(self) -> set[str]:
        return {tenant.id for tenant in self.tenants}


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _role(value: Any, name: str) -> str:
    role = str(value or "viewer").strip().lower()
    if role not in ROLE_ORDER:
        raise ValueError(f"{name} must be one of {', '.join(ROLE_ORDER)}")
    return role


def _string_list(value: Any, *, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError("expected a string or list of strings")
    return [str(item) for item in value]


def _tenant_map(value: Any, name: str) -> dict[str, list[str]]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return {str(key): _string_list(item) for key, item in value.items()}


def _role_map(value: Any, name: str) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return {str(key): _role(item, f"{name}.{key}") for key, item in value.items()}


def load_enterprise_config(path: str | Path) -> EnterpriseConfig:
    core = load_config(path)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    tenant_rows = raw.get("tenants") or []
    tenants: list[TenantConfig] = []
    if tenant_rows:
        if not isinstance(tenant_rows, list):
            raise ValueError("tenants must be a list")
        for item in tenant_rows:
            if not isinstance(item, dict):
                raise ValueError("each tenant must be a mapping")
            tenant_id = str(item.get("id") or "").strip()
            if not tenant_id:
                raise ValueError("tenant.id is required")
            if tenant_id == "*":
                raise ValueError("tenant id '*' is reserved for authorization scope")
            tenants.append(TenantConfig(
                id=tenant_id,
                name=str(item.get("name") or tenant_id),
                platforms=_string_list(item.get("platforms"), default=["*"]),
            ))
    else:
        tenants = [TenantConfig(id="default", name="Default", platforms=["*"])]

    ids = [tenant.id for tenant in tenants]
    if len(ids) != len(set(ids)):
        raise ValueError("tenant ids must be unique")

    # Make platform ownership unambiguous. A tenant boundary that overlaps is a
    # confidentiality bug, not merely a configuration warning.
    for platform in core.platforms:
        matches = [
            tenant.id
            for tenant in tenants
            if any(fnmatch.fnmatch(platform.name, pattern) for pattern in tenant.platforms)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"platform {platform.name!r} must match exactly one tenant; matched {matches or 'none'}"
            )

    identity_raw = raw.get("identity") or {}
    oidc_raw = identity_raw.get("oidc") or {}
    oidc_enabled = bool(oidc_raw.get("enabled", False))
    issuer = str(oidc_raw.get("issuer") or "").rstrip("/")
    client_id = str(oidc_raw.get("client_id") or "")
    redirect_uri = str(oidc_raw.get("redirect_uri") or "")
    if oidc_enabled:
        if not issuer.startswith("https://"):
            raise ValueError("identity.oidc.issuer must be an https URL")
        if not client_id:
            raise ValueError("identity.oidc.client_id is required when OIDC is enabled")
        if not redirect_uri.startswith("https://") and not redirect_uri.startswith("http://127.0.0.1") and not redirect_uri.startswith("http://localhost"):
            raise ValueError("identity.oidc.redirect_uri must use https except for loopback development")

    oidc = OIDCConfig(
        enabled=oidc_enabled,
        issuer=issuer,
        client_id=client_id,
        client_secret_env=(str(oidc_raw.get("client_secret_env")) if oidc_raw.get("client_secret_env") else None),
        redirect_uri=redirect_uri,
        session_secret_env=str(oidc_raw.get("session_secret_env", "IMMUTAVAULT_OIDC_SESSION_SECRET")),
        session_minutes=_bounded_int(oidc_raw.get("session_minutes", 60), "identity.oidc.session_minutes", minimum=5, maximum=720),
        require_mfa=bool(oidc_raw.get("require_mfa", True)),
        allowed_tenant_ids=_string_list(oidc_raw.get("allowed_tenant_ids")),
        required_acrs=_string_list(oidc_raw.get("required_acrs")),
        allow_local_tokens=bool(oidc_raw.get("allow_local_tokens", False)),
        default_role=_role(oidc_raw.get("default_role", "viewer"), "identity.oidc.default_role"),
        default_tenants=_string_list(oidc_raw.get("default_tenants")),
        group_role_map=_role_map(oidc_raw.get("group_role_map"), "identity.oidc.group_role_map"),
        group_tenant_map=_tenant_map(oidc_raw.get("group_tenant_map"), "identity.oidc.group_tenant_map"),
        app_role_map=_role_map(oidc_raw.get("app_role_map"), "identity.oidc.app_role_map"),
        app_role_tenant_map=_tenant_map(oidc_raw.get("app_role_tenant_map"), "identity.oidc.app_role_tenant_map"),
    )

    configured_tenants = set(ids)
    for scope_name, scopes in (
        ("identity.oidc.default_tenants", oidc.default_tenants),
        *[(f"identity.oidc.group_tenant_map.{key}", value) for key, value in oidc.group_tenant_map.items()],
        *[(f"identity.oidc.app_role_tenant_map.{key}", value) for key, value in oidc.app_role_tenant_map.items()],
    ):
        invalid = [item for item in scopes if item != "*" and item not in configured_tenants]
        if invalid:
            raise ValueError(f"{scope_name} references unknown tenant(s): {invalid}")

    obs_raw = raw.get("observability") or {}
    metrics_path = str(obs_raw.get("metrics_path", "/metrics"))
    if not metrics_path.startswith("/") or "?" in metrics_path:
        raise ValueError("observability.metrics_path must be an absolute URL path")
    websocket_port = _bounded_int(obs_raw.get("websocket_port", 8788), "observability.websocket_port", minimum=1, maximum=65535)
    if websocket_port == core.portal.port and bool(obs_raw.get("websocket_enabled", True)):
        raise ValueError("observability.websocket_port must differ from portal.port")
    observability = ObservabilityConfig(
        metrics_enabled=bool(obs_raw.get("metrics_enabled", True)),
        metrics_path=metrics_path,
        metrics_token_env=str(obs_raw.get("metrics_token_env", "IMMUTAVAULT_METRICS_TOKEN")),
        websocket_enabled=bool(obs_raw.get("websocket_enabled", True)),
        websocket_listen=str(obs_raw.get("websocket_listen", "127.0.0.1")),
        websocket_port=websocket_port,
        websocket_poll_seconds=_bounded_int(obs_raw.get("websocket_poll_seconds", 2), "observability.websocket_poll_seconds", minimum=1, maximum=60),
        websocket_ticket_ttl_seconds=_bounded_int(obs_raw.get("websocket_ticket_ttl_seconds", 60), "observability.websocket_ticket_ttl_seconds", minimum=15, maximum=300),
        websocket_allowed_origins=_string_list(obs_raw.get("websocket_allowed_origins")),
        include_platform_labels=bool(obs_raw.get("include_platform_labels", True)),
    )

    user_tenants: dict[str, list[str]] = {}
    portal_raw = raw.get("portal") or {}
    for item in portal_raw.get("users", []) or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        scopes = _string_list(item.get("tenants"), default=["*"])
        invalid = [scope for scope in scopes if scope != "*" and scope not in configured_tenants]
        if invalid:
            raise ValueError(f"portal user {item['name']!r} references unknown tenant(s): {invalid}")
        user_tenants[str(item["name"])] = scopes

    return EnterpriseConfig(
        core=core,
        tenants=tenants,
        oidc=oidc,
        observability=observability,
        access=EnterpriseAccessConfig(user_tenants=user_tenants),
    )
