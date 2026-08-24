# VMware backup transport

Immutavault v0.7.1 supports two VMware backup policies:

1. native VDDK/CBT through an externally installed, authorized provider/helper; and
2. the proven `hot-clone-export` full-image workflow.

Broadcom VDDK is **not bundled or redistributed** by Immutavault. Native incremental operation requires a compatible `immutavault-vddk` helper implementing protocol version 1.

## Recommended enterprise policy: strict native incremental

Use strict mode when the operational requirement is that every successful scheduled VMware recovery point must genuinely come from the native incremental path.

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: vddk
    include:
      - "*"
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_strict: true
      incremental_fallback: false
      incremental_cache_root: /var/cache/immutavault/vddk
      incremental_block_size: 134217728
      quiesce: true
      quiesce_fallback_crash_consistent: false
      vddk_transport_order:
        - san
        - hotadd
        - nbdssl
```

With `incremental_strict: true`, all native incremental failures fail the backup closed. `incremental_fallback` does not permit automatic full fallback while strict mode is active.

```text
CBT/VDDK healthy + valid change IDs
        -> native incremental backup

anything unavailable / unsafe / malformed / ambiguous
        -> BACKUP FAILED
        -> no hot-clone/OVF fallback
        -> no fallback recovery point
        -> no change-ID advancement from an uncertain run
```

Examples that fail in strict mode include:

- helper missing;
- capability probe failure;
- CBT disabled or uninitialized;
- CBT unsupported or invalid;
- change-ID/generation reset;
- invalid change ID;
- unsupported disk;
- baseline required;
- missing or corrupt checkpoint/layout;
- unknown provider reason;
- provider omission of `fallback_safe`;
- invalid provider JSON;
- unexpected provider exception/crash.

## Provider protocol contract

The helper is invoked for `capabilities`, `backup`, and `restore` operations. It must report protocol version 1 and the `cbt`, `backup`, and `restore` features before Immutavault considers it available.

Credentials are inherited through the VMware environment and are not placed in command arguments. Backup requests contain the VM identity, destination cache, previous per-disk checkpoint, block size, transport preference order and quiesce setting.

A successful backup must:

- exit successfully;
- return JSON with `status: success`;
- create `immutavault-vddk-layout.json` in the destination cache; and
- return a non-empty per-disk checkpoint object.

Immutavault writes the checkpoint atomically and records transport metadata in `.immutavault-transport.json`.

## Cache safety

The native cache defaults to:

```text
/var/cache/immutavault/vddk/<platform>/<vm>/
```

The cache contains recoverable guest data and is created with restrictive permissions. A completed backup is exposed to the normal staging/repository path through a snapshot view so unchanged local block files can be hard-linked where the filesystem permits it.

If the provider has entered the backup operation and then fails, Immutavault removes the per-VM native cache before making any fallback decision. The next native run therefore cannot silently trust a potentially partially modified CBT chain.

Strict mode still invalidates the uncertain cache, but it **does not** start a full fallback.

## Controlled non-strict fallback

Non-strict mode is optional. It is intended only for environments where a fresh full backup is operationally acceptable for narrowly understood conditions.

```yaml
mode: auto
options:
  incremental_strict: false
  incremental_fallback: true
```

Fallback is not open-ended. Immutavault requires all applicable policy checks to pass.

### Capability-stage fallback reasons

Only these pre-provider conditions are allow-listed:

- `helper_missing`
- `missing_required_capability`

These are detected before the provider is allowed to modify the CBT cache.

### Provider-stage fallback reasons

After backup has started, fallback requires both an allow-listed reason **and** an explicit `fallback_safe: true` from the provider.

Allow-listed provider-stage reasons are:

- `cbt_disabled`
- `cbt_uninitialized`
- `cbt_not_supported`
- `cbt_invalid`
- `change_id_reset`
- `invalid_change_id`
- `unsupported_disk`
- `baseline_required`

Example of an eligible provider result:

```json
{
  "status": "fallback",
  "reason": "change_id_reset",
  "fallback_safe": true
}
```

These examples fail closed:

```json
{"status":"fallback","reason":"change_id_reset"}
```

`fallback_safe` was omitted, so the state is unsafe by default.

```json
{"status":"fallback","reason":"provider_error","fallback_safe":true}
```

The reason is not allow-listed.

```json
{"status":"fallback","reason":"checkpoint_corrupt","fallback_safe":true}
```

The provider cannot override Immutavault's allowlist.

Unexpected exceptions are ambiguous by definition and never become automatic full backups.

## Explicit full backup mode

`hot-clone-export` remains a supported full-image transport and can be selected directly:

```yaml
mode: hot-clone-export
options:
  quiesce: true
  quiesce_fallback_crash_consistent: false
```

The workflow is:

1. create a short-lived source snapshot;
2. request VMware Tools quiescing when configured;
3. create a powered-off temporary clone from the snapshot;
4. export the temporary clone as OVF/VMDK;
5. destroy the temporary clone;
6. remove/consolidate the source snapshot; and
7. commit the exported payload to the encrypted append-only repository.

The protected VM is not deliberately powered off. Snapshot/quiesce operations can still cause a brief stun, and application-consistent quiescing depends on VMware Tools and the guest/application.

The temporary clone requires datastore capacity during export. Monitor free datastore capacity and snapshot consolidation health.

## Restore behavior

Native VDDK/CBT recovery points require the authorized provider/helper to be available for restore. Immutavault refuses implicit overwrite of an existing target VM. Always restore under a new recovery name and boot in an isolated network before production cutover.

A full `hot-clone-export` recovery point continues to use the normal VMware import path.

## Doctor and dry-run expectations

Before enabling schedules:

```bash
immutavault --config /etc/immutavault/immutavault.yml doctor
immutavault --config /etc/immutavault/immutavault.yml inventory
immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run
```

In strict mode, an unavailable or unsafe native provider should be visible as a failure, not disguised as a successful full-backup plan.

## Production acceptance

On a disposable VM first, prove all of the following:

- vCenter CA trust is valid and `insecure: false`;
- the helper resolves and reports protocol-v1 capabilities;
- the first native baseline succeeds;
- a second backup reads only changed ranges according to provider telemetry;
- per-disk checkpoint/change IDs advance only after successful native completion;
- strict mode fails when the helper is removed or CBT is disabled;
- strict failure does not create a hot-clone fallback recovery point;
- an in-flight injected provider failure invalidates the native cache;
- the following run rebuilds/recovers from a valid native baseline as appropriate;
- recovery-point verification passes;
- native restore to a new VM name succeeds;
- the restored VM boots isolated and passes application checks.

If non-strict fallback is intentionally used, separately prove that an allow-listed reason with `fallback_safe: true` can produce a **clearly marked full fallback**, while an unknown reason or omitted `fallback_safe` fails closed.

## Boundary

VDDK/CBT provides backup incrementality; it is not continuous data protection. Achievable RPO is still bounded by the schedule, VMware/provider performance, and successful completion of each protected VM job.
