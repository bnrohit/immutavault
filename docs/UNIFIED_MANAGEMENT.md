# Unified Management — v1.1

Immutavault v1.1 makes the authenticated enterprise portal the normal deployment and day-2 control surface while preserving the CLI and validated YAML as equivalent automation interfaces.

## Trust boundary

The portal runs as the unprivileged `immutavault` identity. It does not mount filesystems, write `/etc`, generate systemd units, or receive `CAP_SYS_ADMIN`.

Two local brokers hold narrow privilege:

- `immutavault-flr.service` owns read-only FUSE/libguestfs file-level recovery mounts.
- `immutavault-management.service` owns validated configuration writes, named-policy timer units, and administrator-requested NFS/SMB mounts.

Both brokers use Unix sockets and Linux `SO_PEERCRED`. A network request cannot connect directly to them. Portal management routes additionally require an authenticated global admin (`role=admin` and tenant scope `*`).

## Guided onboarding

Use **Setup & Manage** in this order:

1. Add a VMware/vCenter, Proxmox, or XCP-ng platform.
2. Click **Test + discover** before saving.
3. Discover inventory and select exact VMs.
4. Add/test storage.
5. Create a named protection policy.
6. Run `doctor` and a policy dry-run.
7. Run the first real backup.
8. Verify the recovery point and immutable copies.
9. Perform an isolated restore/DR test before normal production scheduling.

A candidate config must pass the complete v1.1 schema before it can replace the live file. This includes enterprise tenant ownership. The management service keeps a backup copy and restores the previous configuration if post-write validation fails.

## Credentials

The browser can accept credentials during an administrative setup request, but values are written to `/etc/immutavault/immutavault.env`, not into YAML. YAML stores environment-variable references.

The environment file remains `root:immutavault` and is not returned through portal APIs. OIDC client secrets, hypervisor credentials, object-storage credentials and repository passwords follow the same rule.

## Named protection policies

A policy contains:

- stable lowercase ID;
- display name;
- enabled/disabled state;
- one or more exact platform/VM selections;
- manual, daily, weekly or hourly schedule;
- immutable days;
- explicit replica targets;
- post-backup verification flag.

Wildcards are prohibited in checkbox-generated policy VM selections. This prevents a UI selection from unexpectedly expanding after new VMs appear.

An empty replica list means **primary repository only**. Every selected replica must name an enabled configured replica.

Scheduled policies create `immutavault-policy-<id>.timer`, pointing to `immutavault-policy@<id>.service`. The worker runs as `immutavault`, not root. Saving the first scheduled named policy disables the broad legacy `immutavault-backup.timer` to avoid duplicate backups.

Useful CLI equivalents:

```bash
immutavault --config /etc/immutavault/immutavault.yml policy-list
immutavault --config /etc/immutavault/immutavault.yml policy-dry-run --name daily-production
immutavault --config /etc/immutavault/immutavault.yml policy-run --name daily-production
```

## NFS and SMB onboarding

The portal supports both existing mounted filesystem paths and administrator-managed NFS/SMB mounts.

Managed mounts must be below:

```text
/srv/immutavault/storage
```

Examples:

```text
NFS:  truenas.example.local:/mnt/tank/immutavault
SMB:  //fileserver.example.local/immutavault
```

NFS test options use hard mounts plus `nosuid,nodev`. SMB uses SMB 3.1.1 with `nosuid,nodev,noexec`; credentials are stored in a root-owned file under `/etc/immutavault`, not inside the generated `.mount` unit.

The service first mounts the share temporarily, verifies it is mounted and writable, unmounts it, then creates/enables a persistent systemd mount unit only when the administrator chooses Save.

For Dell/TrueNAS storage exposed as standard NFS/SMB, this workflow is sufficient. v1.1 does **not** claim a proprietary Dell or TrueNAS array-management API integration unless such an integration is separately implemented and tested.

## Replica selection

Protection policy destination semantics are explicit:

```text
Primary repository     always receives the backup
Selected replicas      receive copies
No selected replicas   primary only
```

The portal displays configured enabled replicas as checkboxes. It never treats an empty selection as “all replicas.”

## Browser file recovery

Select a recovery point and choose **Files**. The portal requests an owner-bound session from the FLR broker. Directory browsing stays read-only and a file is downloadable only when the FLR safety layer marks it as a regular, permitted file within the configured size limit.

The portal never follows guest symlinks to escape the mounted guest filesystem.

## Isolated DR test

The administrator first registers an isolated network for a specific target platform. Registration itself performs live target validation; a string cannot simply be added to an allow-list if the network/bridge does not exist.

A restore operator then chooses **Run DR Test** from a verified recovery point. The request goes through the normal restore request/four-eyes workflow. At request time and again at execution, Immutavault checks:

- source recovery point exists;
- recovery point is verified;
- recovery point is not anomaly-flagged;
- target is within the user's tenant scope;
- target tenant matches the source tenant;
- target network is explicitly allow-listed;
- the network still exists on the target;
- any cross-hypervisor path is certified by the v1.0 V2V engine.

After restore, every NIC is remapped while the VM is powered off. The VM is booted only on the isolated network. A running-state check is performed after `management.dr_test_boot_seconds`. Then it is powered off and, by default, destroyed.

If required cleanup fails, the test is marked failed and the cleanup error is audited. The source production VM is never powered off, renamed, reconfigured or deleted by this test.

## Production DR remains separate

An isolated test never activates the DR routed gateway, production IP ownership, VXLAN/OSPF promotion or primary fencing. Production promotion still uses the DR runbook and explicit fencing controls.

`immutavault-dr-watch.timer` is deliberately not enabled by generic installation.

## Acceptance checklist

Before calling the deployment production-ready:

1. OIDC/MFA and tenant roles work as intended.
2. Global-admin setup routes are inaccessible to tenant admins.
3. Management and FLR sockets reject unauthorized local UIDs.
4. Hypervisor Test + Discover passes.
5. Storage test passes.
6. Policy dry-run contains only the exact selected VMs.
7. A real backup lands in the primary immutable repository.
8. Selected replicas copy successfully and immutability is verified.
9. FLR can recover a disposable file.
10. Full-VM restore creates a new VM and never overwrites an existing one.
11. An isolated DR test boots on a non-production network and cleans itself up.
12. Audit-chain verification passes after the workflow.
13. Production DR promotion remains disabled until a separate failover/failback drill succeeds.
