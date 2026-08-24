# Security Model

## Primary threat

The main ransomware scenario is compromise of a VM, hypervisor administrator, backup controller, or backup-writer credential followed by an attempt to erase recovery points. The network-exposed repository endpoint is append-only, so the ordinary backup writer cannot prune or delete existing repository objects.

## OS identity separation

- `immutavault`: backup controller, recovery portal, staging access.
- `immutavault-store`: append-only repository daemon and repository filesystem access. Its systemd unit receives only `/etc/immutavault/repository.env` (repository root), **not** the controller environment containing restic encryption, portal, cloud, or hypervisor credentials.
- `root`: retention/prune authority; not exposed as a network backup credential.

The repository daemon and portal use separate TLS private keys.

## Recovery authorization

Portal roles are:

- `viewer` — read catalog only.
- `restore_operator` — choose a recovery point and request/execute restores allowed by policy.
- `approver` — approve another operator's request.
- `admin` — full recovery-control-plane access and audit visibility.

When `require_four_eyes_restore` is true, a requester cannot approve their own restore request.

Portal tokens can be restricted by source-platform glob and VM-name glob. Tokens are supplied by environment variables instead of being written into YAML. The portal refuses a non-loopback plaintext listener; remote exposure requires TLS. Generic internal exceptions are logged server-side instead of being returned verbatim to clients.

## Non-destructive recovery default

Every restore adapter checks for an existing target VM name/ID and refuses automatic overwrite. Recovery imports a new VM unless an administrator manually performs a later cutover.

## Integrity

- restic cryptographic repository integrity protects stored chunks.
- each recovery point includes an Immutavault manifest.
- small files receive SHA-256 hashes; large VM payloads rely on restic's chunk integrity plus file size/metadata in the manifest.
- `verify-point` performs a real staging restore and validates the manifest.
- scheduled repository verification reads a configured percentage of repository data.

## Retention hardening

The root maintenance task obtains the same global lock as backup jobs. It previews restic retention in JSON, excludes state-protected recovery points, then deletes only eligible snapshot IDs. It uses `--keep-within` plus GFS history and a minimum restore-point count.

## Ransomware/anomaly signal

Immutavault compares repository data added and backup-size changes against configurable thresholds. Suspicious points are surfaced in the catalog and can receive a longer catalog protection window.

This is a heuristic, not malware scanning. Application-aware clean-room scanning should be added for environments that require malware classification before recovery.

## Root/physical compromise

No ordinary software on one server can make local disks indestructible from an attacker with unrestricted root or physical control. Stronger designs need a second administrative/failure boundary such as object lock, storage-array retention lock, offline media, or a remote vault whose destructive credentials are inaccessible from production.

## Recommended hardening

- Dedicated physical vault server on a management/backup network.
- MFA and separate admin accounts for BMC and OS administration.
- Restrict rest-server ingress to backup controller sources.
- Restrict portal ingress to trusted management/customer networks or reverse proxy with enterprise SSO.
- SSH keys only; disable direct root SSH.
- Send logs/audit events to an external SIEM/syslog system.
- Monitor repository and staging free space.
- Run isolated test restores regularly.
- Keep at least one copy in another failure domain.


## Tamper-evident control-plane audit

Every audit event includes the previous event hash and its own SHA-256 event hash. `immutavault audit-verify` walks the entire chain and reports alteration or deletion/reordering that breaks the chain. This is tamper-evident, not magical tamper-proof storage: a root attacker who can rewrite both the database and every hash can still forge a new chain, so replicate/export audit records to a separately administered security system for stronger assurance.

## Recovery-point holds

The administrative hold API is deliberately one-way: it can extend `immutable_until` but cannot shorten it. Retention excludes all active held points. This prevents a normal portal or operator workflow from weakening an existing immutable window.
