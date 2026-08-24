# Restore and Recovery Runbook

Immutavault restores by recovery-point ID. A recovery point is immutable metadata plus a restic snapshot containing the exported VM payload and an integrity manifest.

## Safe workflow

1. User selects a recovery point in the portal or CLI.
2. Immutavault creates a restore request.
3. If four-eyes policy is enabled, a different identity approves the request.
4. The selected restic snapshot is restored into isolated staging.
5. `.immutavault-manifest.json` is validated.
6. The target adapter checks that the new target name/ID does not already exist.
7. The workload is imported as a new VM.
8. The event is written to the audit database.
9. Staging is deleted after success.

## VMware

The v0.5 VMware path stores OVF/VMDK produced from the snapshot-assisted powered-off temporary clone workflow using `govc`. Restore locates the OVF descriptor and uses `govc import.ovf -name <new-name>`. Configure GOVC datacenter/datastore/network/resource-pool environment as appropriate for the destination.

## Proxmox

The fallback stores `vzdump` archives. Recovery uses `qmrestore` for QEMU VMs and `pct restore` for containers. If `vmid` is not supplied in restore options, the adapter asks the cluster for the next available ID. Existing VMIDs are not overwritten.

Example restore options:

```json
{"vmid":"501","storage":"local-lvm"}
```

## XCP-ng

Recovery copies the XVA to the selected XCP-ng pool master and calls `xe vm-import`. `sr_uuid` can be supplied in the request or configured as the platform default. Existing same-name VMs are not overwritten automatically.

Example:

```json
{"sr_uuid":"YOUR-SR-UUID"}
```

## Cross-hypervisor restore

The safe core blocks cross-hypervisor restore. Disk conversion alone does not guarantee bootability because firmware mode, controllers, guest drivers, networks, guest tools, snapshots and application consistency differ. Implement a conversion plugin only after testing the exact source/target combination.
