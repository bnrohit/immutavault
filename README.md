# Immutavault v0.9.0

Immutavault is an open, vendor-neutral **immutable VM backup, recovery, replication, file-level recovery and disaster-recovery orchestrator** for VMware/vCenter, Proxmox VE and XCP-ng.

The security principle remains simple: **the identity that creates backups does not receive prune/delete authority.** The normal controller writes through an append-only repository service; retention/prune is separated. Recovery is similarly conservative: restore operations create a **new VM** and refuse implicit production overwrite.

> **Readiness statement:** v0.9.0 is a production-pilot candidate that adds enterprise tenant isolation, Microsoft Entra ID / OIDC with explicit MFA evidence enforcement, Prometheus metrics and real-time WebSocket operations telemetry on top of the v0.8 granular recovery and v0.7.1 fail-closed native-incremental core. Production acceptance on the actual hypervisors, storage, identity provider and monitoring stack is still required before go-live.

## v0.9 — Enterprise Operations & Ecosystem

### Multi-tenant / multi-site authorization

- Each hypervisor platform belongs to exactly one configured tenant.
- Overlapping or unassigned tenant patterns fail configuration validation.
- Portal VM/recovery-point views are tenant-scoped.
- FLR sessions are tenant-scoped through the recovery point that created them.
- Restore requests, approval and execution are tenant-scoped.
- Cross-tenant restore targets are rejected.
- WebSocket operations telemetry is filtered to the authenticated tenant scopes.
- Full audit/system-health endpoints require a global admin (`admin` + `*` tenant scope).

### OIDC / Microsoft Entra ID

v0.9 provides a native authorization-code + PKCE OIDC flow using the appliance's existing Python/OpenSSL runtime. Use a **tenant-specific Entra v2.0 issuer** and exact callback URI.

The portal validates:

- signed login state;
- PKCE verifier;
- RS256 JWT signature and JWKS `kid`;
- exact discovery issuer;
- audience/client ID;
- token time bounds;
- OIDC nonce;
- optional Entra directory tenant allowlist;
- MFA evidence (`amr` containing `mfa`/`ngcmfa`) or explicitly configured Authentication Context IDs (`acrs`).

A valid Entra login without the configured MFA evidence fails closed when `require_mfa: true`.

Role and tenant mapping support both Entra group object IDs and app-role claims. This enables patterns such as an `approver` for Campus A who cannot view or approve Campus B recovery operations.

### Prometheus / Grafana / Datadog / PagerDuty

`/metrics` exposes OpenMetrics-compatible low-cardinality telemetry including:

- RPO compliance;
- latest successful backup age;
- successful/failed backup-job counters;
- total/verified/suspicious recovery points;
- immutable-copy status and verification;
- restore-request states;
- DR run results;
- tamper-evident audit-chain validity.

Prometheus labels tenant/platform/status/target but deliberately **do not include VM names or guest file paths**. Grafana can use Prometheus directly; Datadog Agent can use its OpenMetrics integration; PagerDuty is best connected through Prometheus Alertmanager. Example alert rules are in `ops/prometheus/immutavault-alerts.yml`.

### Real-time WebSocket operations

The enterprise portal can start a dedicated RFC 6455 WebSocket listener. The browser first obtains a short-lived HMAC-signed ticket from the authenticated HTTPS session; long-lived portal credentials aren't placed in the WebSocket URL.

The live stream provides tenant-filtered recent/running jobs and recovery summary data. Running percentages are explicitly marked as **estimates** because the supported hypervisors don't expose one common progress API. A job reaches successful 100% only when the authoritative state database records success.

See `docs/ENTERPRISE_OPERATIONS.md` and `config/enterprise-v0.9.example.yml`.

## Existing protection capabilities retained in v0.9

- **Granular file-level recovery:** read-only restic FUSE + libguestfs browsing/downloads without full VM import first.
- Application-consistency metadata in the immutable recovery payload and catalog.
- `application_consistency_strict: true` for VMware protection.
- VMware native VDDK/CBT through an externally installed authorized helper.
- `incremental_strict: true` with `incremental_fallback: false` for fail-closed enterprise native incremental protection.
- Explicit `hot-clone-export` full VMware backup path.
- Proxmox online `vzdump --mode snapshot`, `qmrestore` / `pct restore` safety guards.
- XCP-ng snapshot/XVA backup and recovery path.
- Encrypted deduplicated restic repositories.
- Authenticated TLS append-only repository writer endpoint.
- GFS retention with protected immutable windows.
- SHA-256 manifest verification and staged recovery-point verification.
- Backup-churn anomaly detection and suspicious-point preservation.
- Tamper-evident SHA-256 audit chain.
- Four-eyes restore approval.
- S3-compatible replicas and provider immutability support where available.
- Cloudflare R2 Bucket Lock kept distinct from S3 Object Lock.
- NFS/SMB/filesystem replicas.
- Online SQLite control-plane backups.
- Versioned atomic application upgrades/rollback.
- Multi-site DR orchestration, fencing, VXLAN recovery networks and FRR/OSPF ownership controls.
- Cross-hypervisor automatic conversion blocked until separately certified.

## VMware strict native incremental example

Broadcom VDDK is **not bundled** or redistributed. Install an authorized compatible `immutavault-vddk` helper separately.

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: vddk
    include: ["*"]
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_strict: true
      incremental_fallback: false
      incremental_cache_root: /var/cache/immutavault/vddk
      quiesce: true
      quiesce_fallback_crash_consistent: false
      application_consistency_strict: true
      vddk_transport_order: [san, hotadd, nbdssl]
```

Strict mode means helper absence, unsafe CBT state, malformed provider output, invalid checkpoints or unexpected provider exceptions fail the backup. The system doesn't silently create a hot-clone recovery point and call it incremental.

## Enterprise identity + tenancy example

```yaml
tenants:
  - id: campus-a
    name: Campus A
    platforms: [vc-campus-a, pve-campus-a]
  - id: campus-b
    name: Campus B
    platforms: [vc-campus-b]

identity:
  oidc:
    enabled: true
    issuer: https://login.microsoftonline.com/<TENANT-ID>/v2.0
    client_id: <APP-CLIENT-ID>
    client_secret_env: IMMUTAVAULT_ENTRA_CLIENT_SECRET
    redirect_uri: https://backup.example.com/auth/callback
    require_mfa: true
    allow_local_tokens: false
    group_role_map:
      "<admin-group-object-id>": admin
      "<approver-group-object-id>": approver
      "<operator-group-object-id>": restore_operator
    group_tenant_map:
      "<campus-a-group-object-id>": [campus-a]
      "<campus-b-group-object-id>": [campus-b]
```

Use Microsoft Entra Conditional Access / authentication strength as the primary MFA policy. The Immutavault claim check is an additional fail-closed application gate.

## Observability example

```yaml
observability:
  metrics_enabled: true
  metrics_path: /metrics
  metrics_token_env: IMMUTAVAULT_METRICS_TOKEN
  include_platform_labels: true

  websocket_enabled: true
  websocket_listen: 0.0.0.0
  websocket_port: 8788
  websocket_public_url: wss://backup.example.com:8788
  websocket_poll_seconds: 2
  websocket_ticket_ttl_seconds: 60
  websocket_allowed_origins:
    - https://backup.example.com
```

The installer generates `IMMUTAVAULT_OIDC_SESSION_SECRET` and `IMMUTAVAULT_METRICS_TOKEN` for new controllers and adds them non-destructively during upgrades. Entra client secrets are never generated by Immutavault; place the real app-registration secret in the configured environment variable.

## Recommended topology

```text
                     Microsoft Entra ID
                         OIDC + MFA
                             |
                             v
 +----------------+   HTTPS portal/API   +----------------------+
 | NOC / SOC      |--------------------->| Immutavault control  |
 | Grafana        |<--- Prometheus ------| plane / portal       |
 | Datadog        |<--- /metrics --------| + WebSocket ops      |
 +----------------+                      +----------+-----------+
          |                                         |
          | Alertmanager -> PagerDuty               | append-only backup
          |                                         v
          |                              +----------------------+
          |                              | Primary vault        |
          |                              | rest-server + repo   |
          |                              +----------+-----------+
          |                                         |
          |                       immutable replica / DR copy
          |                                         v
          |                              +----------------------+
          +----------------------------->| DR / object / NAS    |
                                         +----------------------+
```

Keep control plane, primary repository and additional immutable copy in separate failure domains when possible.

## Fast install on Ubuntu 24.04 LTS

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
git checkout v0.9.0
sudo ./scripts/preflight.sh
./scripts/release_check.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
sudo ./scripts/launch_setup_console.sh
```

For enterprise configuration, start from:

```text
config/enterprise-v0.9.example.yml
```

Do not enable recurring production schedules until `doctor`, inventory, dry-run, a real backup, recovery-point verification, FLR and an isolated full-VM restore all pass.

## Core commands

```bash
immutavault --config /etc/immutavault/immutavault.yml doctor
immutavault --config /etc/immutavault/immutavault.yml status
immutavault --config /etc/immutavault/immutavault.yml inventory
immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run
immutavault --config /etc/immutavault/immutavault.yml backup --all
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml portal
```

## Important boundaries

- Native VMware VDDK/CBT requires a separately authorized provider/helper; Broadcom VDDK is not bundled.
- VDDK/CBT is incremental backup, not CDP. RPO is bounded by schedule and successful completion.
- Current Proxmox and XCP-ng paths are snapshot/export based, not PBS/XO native incrementals.
- Application consistency depends on guest/application quiescing and must be tested per workload.
- WebSocket running percentages are metadata-based estimates until job completion.
- Prometheus is a monitoring surface, not a restore-control surface.
- Entra/OIDC MFA evidence validation doesn't replace Conditional Access.
- Multi-tenant Immutavault partitions authorization by configured platform ownership; it doesn't make one shared repository cryptographically unique per tenant.
- Cross-hypervisor automatic conversion remains blocked in the safe core.
- Automatic DR is not enabled by installation.

## Documentation

- `docs/ENTERPRISE_OPERATIONS.md` — v0.9 tenancy, Entra/OIDC, Prometheus, WebSockets, Grafana, Datadog and PagerDuty
- `docs/FILE_LEVEL_RECOVERY.md` — v0.8 granular read-only recovery
- `docs/VMWARE_BACKUP.md` — VDDK/CBT, application consistency and fallback policy
- `docs/INCREMENTAL_STRICT_MODE.md` — fail-closed v0.7.1 native incremental policy
- `docs/PRODUCTION_ACCEPTANCE.md` — go-live gates
- `docs/INSTALLATION.md` — installation
- `docs/OPERATIONS.md` — day-2 operations
- `docs/RESTORE.md` — restore runbook
- `docs/DR_RUNBOOK.md` — failover/failback
- `docs/HIGH_AVAILABILITY.md` — HA design
- `docs/SECURITY.md` — security model
- `docs/ARCHITECTURE.md` — components and trust boundaries
- `docs/CLOUD_STORAGE.md` — S3/NFS/SMB targets
- `docs/VEEAM_GAP_MATRIX.md` — enterprise capability comparison

## License

Apache-2.0. See `LICENSE`.
