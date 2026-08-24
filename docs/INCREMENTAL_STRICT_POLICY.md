# Incremental strict-mode safety policy (v0.7.1)

Immutavault v0.7.1 tightens VMware VDDK/CBT fallback behavior so a native incremental provider can never silently push an unsafe or ambiguous state into the full-backup path.

## Strict mode

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: auto
    options:
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_fallback: true
      incremental_strict: true
```

When `incremental_strict: true`, **all native incremental failures fail the backup job closed**. `incremental_fallback` is ignored for failure handling while strict mode is active.

```text
CBT/VDDK healthy
    -> native incremental backup

provider missing / CBT disabled / change-ID reset / provider error
    -> backup FAILED
    -> no hot-clone fallback
    -> no fallback recovery point
    -> no change-ID advancement
```

## Non-strict mode

When strict mode is false, fallback is still not open-ended. Immutavault requires:

1. `incremental_fallback: true`;
2. a known, allow-listed fallback reason; and
3. after provider backup has started, an explicit `fallback_safe: true` from the provider.

Unknown, malformed, ambiguous, or unsafe provider states fail closed even with strict mode disabled.

### Capability-stage reasons allowed to fall back

- `helper_missing`
- `missing_required_capability`

These are detected before the provider is allowed to mutate the CBT cache.

### Provider-stage reasons allowed to fall back only with `fallback_safe: true`

- `cbt_disabled`
- `cbt_uninitialized`
- `cbt_not_supported`
- `cbt_invalid`
- `change_id_reset`
- `invalid_change_id`
- `unsupported_disk`
- `baseline_required`

Any other reason is denied.

## Ambiguous provider behavior

These examples fail closed:

```json
{"status":"fallback","reason":"provider_error","fallback_safe":true}
```

The reason is not allow-listed.

```json
{"status":"fallback","reason":"change_id_reset"}
```

The provider omitted `fallback_safe: true`; omission defaults to unsafe in v0.7.1.

```json
{"status":"fallback","reason":"checkpoint_corrupt","fallback_safe":true}
```

The reason is not allow-listed, so the provider cannot override Immutavault's safety policy.

## Cache handling

If the native provider has already entered the backup operation and then fails, Immutavault invalidates the per-VM native cache before doing anything else. A full fallback, when policy allows one, starts from the proven `hot-clone-export` path and receives a transport marker recording the fallback reason.

With strict mode enabled, the cache is still invalidated after an in-flight provider failure, but no fallback backup is started.

## Recommended enterprise policy

For environments where backup operators require assurance that every scheduled VMware job is genuinely native incremental, use:

```yaml
mode: vddk
options:
  incremental_strict: true
  incremental_fallback: false
```

For mixed environments where a fresh full backup is acceptable for clearly understood CBT reset/unsupported conditions, use:

```yaml
mode: auto
options:
  incremental_strict: false
  incremental_fallback: true
```

Do not enable non-strict fallback merely to hide provider health problems. `doctor` reports unsafe or ambiguous provider capability states instead of treating them as a normal fallback condition.
