# Enterprise Operations & Ecosystem — v0.9

Immutavault v0.9 adds an enterprise operations plane around the existing immutable backup/recovery core:

- tenant ownership for hypervisor platforms;
- tenant-scoped portal authorization;
- Microsoft Entra ID / generic OIDC authorization-code login with PKCE;
- explicit MFA evidence enforcement;
- Prometheus/OpenMetrics-compatible metrics;
- short-lived-ticket WebSocket operations telemetry;
- tenant-aware restore approval and execution checks;
- Grafana, Datadog and PagerDuty integration patterns.

The backup repository security model is unchanged: interactive portal identities do not receive repository prune/delete authority.

## 1. Tenant model

Define logical tenants and assign every configured hypervisor platform to exactly one tenant:

```yaml
tenants:
  - id: campus-a
    name: Campus A
    platforms: [vc-campus-a, pve-campus-a]
  - id: campus-b
    name: Campus B
    platforms: [vc-campus-b]
```

Patterns are allowed, but configuration validation rejects overlap. A platform matching zero or more than one tenant fails closed. This prevents a wildcard mistake from silently exposing one site's VMs to another site's users.

Tenant scope is enforced on:

- VM inventory/catalog views;
- recovery-point browsing;
- FLR session creation;
- restore requests;
- restore approvals;
- restore execution;
- verify/hold operations;
- WebSocket job telemetry.

A restore target must be in the same tenant as the source recovery point. Cross-tenant restore is rejected by the portal even if the hypervisor type is compatible.

The complete audit log and full system-health view require an `admin` identity with the global `*` tenant scope. Tenant administrators do not automatically become global auditors.

## 2. Microsoft Entra ID / OIDC

Use a tenant-specific Microsoft identity platform v2.0 authority:

```yaml
identity:
  oidc:
    enabled: true
    issuer: https://login.microsoftonline.com/<DIRECTORY-TENANT-ID>/v2.0
    client_id: <APP-REGISTRATION-CLIENT-ID>
    client_secret_env: IMMUTAVAULT_ENTRA_CLIENT_SECRET
    redirect_uri: https://backup.example.com/auth/callback
    session_secret_env: IMMUTAVAULT_OIDC_SESSION_SECRET
    require_mfa: true
    allow_local_tokens: false
```

The portal discovers `authorization_endpoint`, `token_endpoint`, `jwks_uri` and the authoritative issuer from `/.well-known/openid-configuration`. It uses the authorization-code flow with PKCE and validates:

1. signed login state and expiry;
2. authorization-code PKCE verifier;
3. JWT `alg` = RS256;
4. signing-key `kid` against current JWKS, with one key-rollover refresh;
5. RS256 signature using the host OpenSSL implementation;
6. exact issuer from discovery;
7. application audience/client ID;
8. `exp` and `nbf`;
9. OIDC nonce;
10. optional allowed Entra directory tenant IDs;
11. MFA evidence when `require_mfa: true`.

The implementation uses only the Python standard library plus the already-required OpenSSL binary. Broad authentication libraries aren't injected into the backup appliance dependency chain.

### MFA requirement

For Microsoft Entra, configure the application/token policy so the ID token contains authentication-method evidence. Immutavault accepts `mfa` or `ngcmfa` in `amr`. You may also configure accepted Entra Authentication Context IDs in `required_acrs`.

```yaml
require_mfa: true
required_acrs:
  - c1
```

If MFA is required and neither accepted `amr` nor configured `acrs` evidence is present, login fails closed. Merely receiving a correctly signed Entra token is not treated as proof of MFA.

Entra Conditional Access should still enforce the organization's actual MFA/authentication-strength policy. Immutavault's claim gate is an application-side verification layer, not a replacement for Conditional Access.

### Role mapping

Map immutable Entra group object IDs or app-role strings to Immutavault roles:

```yaml
group_role_map:
  "<backup-admin-group-object-id>": admin
  "<backup-approver-group-object-id>": approver
  "<backup-operators-group-object-id>": restore_operator

app_role_map:
  Immutavault.Admin: admin
  Immutavault.Approver: approver
  Immutavault.RestoreOperator: restore_operator
```

When multiple mappings apply, the highest role wins in this order:

```text
viewer < restore_operator < approver < admin
```

Use app roles for large Entra environments where group-overage behavior would make group claims inconvenient.

### Tenant mapping

Role and tenant scope are intentionally separate. A user can be an approver for one tenant without seeing another tenant.

```yaml
group_tenant_map:
  "<campus-a-operations-group-object-id>": [campus-a]
  "<campus-b-operations-group-object-id>": [campus-b]
```

An OIDC identity with no tenant mapping is denied unless explicit `default_tenants` are configured.

### Break-glass tokens

Existing portal bearer tokens remain available for migration and emergency access, but OIDC-enabled configurations ignore them by default:

```yaml
allow_local_tokens: false
```

Set it to `true` only for an intentional, monitored break-glass process. Store token secrets only in `/etc/immutavault/immutavault.env`, rotate them under change control, and alert on `auth.oidc.login` / local administrative activity in the audit trail.

## 3. Prometheus metrics

Enable the scrape endpoint:

```yaml
observability:
  metrics_enabled: true
  metrics_path: /metrics
  metrics_token_env: IMMUTAVAULT_METRICS_TOKEN
  include_platform_labels: true
```

For non-loopback scrapes a bearer token is expected. The installer generates `IMMUTAVAULT_METRICS_TOKEN` for new and upgraded controllers. If the token variable is absent, unauthenticated metrics access is limited to loopback.

Example Prometheus scrape job:

```yaml
scrape_configs:
  - job_name: immutavault
    scheme: https
    metrics_path: /metrics
    bearer_token_file: /etc/prometheus/secrets/immutavault.token
    static_configs:
      - targets: [backup.example.com:8787]
    tls_config:
      ca_file: /etc/prometheus/ca/immutavault-ca.crt
```

Important v0.9 metrics include:

- `immutavault_build_info`
- `immutavault_audit_chain_valid`
- `immutavault_recovery_points`
- `immutavault_recovery_points_verified`
- `immutavault_recovery_points_suspicious`
- `immutavault_backup_jobs_total`
- `immutavault_restore_requests`
- `immutavault_last_successful_backup_age_seconds`
- `immutavault_rpo_compliant`
- `immutavault_recovery_copies`
- `immutavault_recovery_copies_verified`
- `immutavault_dr_runs_total`

Metrics use tenant/platform/status/target labels but intentionally do not publish VM names, guest paths, filenames or user identities. This controls cardinality and reduces the risk of leaking customer workload names into a broad monitoring system.

## 4. Grafana

Point Grafana at the Prometheus datasource and build panels around:

- RPO compliance by tenant/platform;
- latest successful backup age;
- failed jobs over time;
- recovery-point verification coverage;
- suspicious recovery points;
- immutable-copy verification;
- DR run success/failure;
- audit-chain validity.

Keep the existing Immutavault recovery portal as the authoritative recovery/action interface. Grafana is the NOC/SOC visibility layer, not a restore-control plane.

## 5. Datadog

Datadog Agent can consume the same endpoint with its OpenMetrics integration. Configure the agent with the HTTPS metrics URL and bearer-token header. Preserve the tenant and platform labels as tags.

Do not add VM names to Prometheus labels just to make Datadog dashboards more detailed. Per-VM live detail belongs in the authenticated WebSocket/portal view where tenant authorization can be enforced.

## 6. PagerDuty

The recommended path is Prometheus -> Alertmanager -> PagerDuty. Example rules are provided in `ops/prometheus/immutavault-alerts.yml`.

High-value pages include:

- audit-chain validation failure;
- RPO breach;
- new failed backup job;
- suspicious recovery point detected;
- missing verified immutable copies.

Route tenant-tagged alerts to the appropriate operations team in Alertmanager. Keep global security alerts such as audit-chain invalidity routed to the central backup/security team.

## 7. Real-time WebSocket operations

The portal starts a dedicated standards-compliant WebSocket listener when enabled:

```yaml
observability:
  websocket_enabled: true
  websocket_listen: 0.0.0.0
  websocket_port: 8788
  websocket_public_url: wss://backup.example.com:8788
  websocket_poll_seconds: 2
  websocket_ticket_ttl_seconds: 60
  websocket_allowed_origins:
    - https://backup.example.com
```

The browser does not put a long-lived portal token in the WebSocket URL. It first requests `/api/v1/ws-ticket` over the authenticated HTTPS session. The ticket:

- is HMAC-signed;
- expires in 15-300 seconds (60 seconds by default);
- contains only the authenticated identity's role and tenant scopes;
- is revalidated by the WebSocket listener;
- cannot expand the user's tenant scope.

The server then sends tenant-filtered operational snapshots with active/recent backup jobs and recovery summary data.

### Progress semantics

Current hypervisor exporters do not expose one common byte-progress interface. v0.9 therefore reports an **estimated** percentage while a job is running by comparing staging-file metadata with the previous protected size. The authoritative `jobs` table alone controls terminal state:

- running export: estimated 5-85%;
- successful committed job: 100%;
- failed job: terminal failed state.

The UI explicitly marks in-progress percentages as estimates. It never reports a successful 100% based solely on observed staging bytes.

## 8. Reverse proxy / load balancer

For HA or centralized TLS, proxy both:

- HTTPS portal/API -> 8787
- WebSocket `/events` -> 8788

Set `websocket_public_url` to the URL the browser can actually reach. Restrict `websocket_allowed_origins` to the portal's public HTTPS origin.

When direct TLS is used, the WebSocket listener reuses the portal certificate/key and enforces TLS 1.2+.

## 9. Zero-Trust operating policy

Recommended production settings:

```yaml
identity:
  oidc:
    enabled: true
    require_mfa: true
    allow_local_tokens: false
observability:
  metrics_enabled: true
  websocket_enabled: true
```

Also enforce:

- tenant-specific Entra authority;
- Conditional Access / authentication strength;
- dedicated Entra groups or app roles;
- least-privilege tenant mappings;
- HTTPS only for non-loopback portal traffic;
- WSS only for browser-accessible WebSockets;
- metrics bearer token stored outside dashboards;
- four-eyes restore approval;
- repository append-only writer separation;
- regular audit-chain validation;
- periodic isolated restore testing.

## 10. Acceptance checklist

Before v0.9 production enablement:

1. Validate `config/enterprise-v0.9.example.yml` against the intended site layout.
2. Confirm every enabled platform resolves to exactly one tenant.
3. Register the Entra web application and exact callback URI.
4. Configure Entra groups/app roles and tenant mappings.
5. Enforce Conditional Access MFA/authentication strength.
6. Confirm a token without MFA evidence is rejected.
7. Confirm a Campus A user cannot list Campus B VMs/recovery points/jobs.
8. Confirm a Campus A restore cannot target Campus B.
9. Confirm the approver can approve only requests inside their tenant scope.
10. Scrape `/metrics` with the dedicated metrics bearer token.
11. Confirm metrics contain no VM-name labels.
12. Connect the portal live-operations WebSocket and verify tenant filtering.
13. Confirm an expired/tampered WebSocket ticket is rejected.
14. Load the supplied Prometheus alert rules.
15. Test Grafana/Datadog dashboards and PagerDuty routing.
16. Run the normal Immutavault production acceptance suite, including real backup, verification, FLR and isolated full-VM restore.

## Microsoft references

- Microsoft identity platform OIDC: https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc
- ID token claims: https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
- Access token / issuer validation guidance: https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens
- Access-token claims and `amr`/`acrs`: https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference
