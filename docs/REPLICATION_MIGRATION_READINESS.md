# Backup, replication and migration: production-readiness gates

This is a development hardening guide for Immutavault, **not a certificate of
production readiness**. The branch contains tests and a new replica readback
verifier, but no vendor-certified live/warm cluster migration engine.

## Confirmed feature boundaries

| Operation | Implementation status | Trust boundary |
| --- | --- | --- |
| Backup to primary append-only restic vault | Existing v1.1 | Depends on real repo ACLs, credentials and restore tests |
| Copy snapshot to a replica | Existing v1.1 | Copy/snapshot listing != restore proof |
| Verify primary recovery point | Existing v1.1 | Manifest restore and file checks |
| Replica actual-restore audit | **Added on this branch** | Restores from the named replica into 0700 temporary storage, checks exact manifest digest, deletes temporary data, records verification |
| Replica readiness and fleet coverage | **Added on this branch** | Report current catalog verification evidence |
| DR promotion gate | **Hardened on this branch** | Copy must be successfully restored and verified within 24 hours, after most recent copy |
| XCP-ng cluster staging | Development branch | Requires approved request, distinct approver, live memory and SR/NIC gates; target remains powered off |
| Final sync/fencing/power-on/rollback | NOT IMPLEMENTED as automated cluster cutover | Manual signed change procedure and pilot required |
| VMware migration execution | NOT IMPLEMENTED | Use supported vCenter mechanisms and test |

## Operator commands

Use the ordinary Immutavault controller with the same protected config and
repository credentials as normal backup jobs. Do not paste secrets into chat or
command history.

```bash
# Controller and source vault health
sudo immutavault --config /etc/immutavault/immutavault.yml doctor
sudo immutavault --config /etc/immutavault/immutavault.yml audit-verify

# Confirm point identity and verification
sudo immutavault --config /etc/immutavault/immutavault.yml recovery-points

# Preview temporary readback; no restore until --execute-readback
sudo immutavault --config /etc/immutavault/immutavault.yml \
  replica-verify --snapshot SNAPSHOT_ID --name REPLICA_NAME --actor admin

# Explicitly read all backed-up bytes FROM the selected replica,
# restore into private temporary space, compare immutable manifest and delete temp.
sudo immutavault --config /etc/immutavault/immutavault.yml \
  replica-verify --snapshot SNAPSHOT_ID --name REPLICA_NAME \
  --actor admin --execute-readback

# Review one snapshot and fleetwide coverage (read-only)
sudo immutavault --config /etc/immutavault/immutavault.yml \
  replica-readiness --snapshot SNAPSHOT_ID --name REPLICA_NAME
sudo immutavault --config /etc/immutavault/immutavault.yml \
  replica-coverage --limit 100
```

Actual-restore verification can consume substantial I/O, bandwidth and
temporary capacity. Run it on a controller with sufficient storage and during
an approved window. A completed restic copy is **not** labeled verified until
readback and file/manifest verification succeed. Any failure revokes that
copy's verification flag. DR promotion now fails closed if the last verification
is missing, older than 24 hours or predates the most recent replica copy.

The verifier does **not** create any VM or verify VM bootability. It does not
prove provider-side S3 Object Lock/WORM, SAN multipath, physical isolation,
application consistency or guest functionality. Test these separately.

## GMCS cluster migration acceptance

1. Ensure new XCP-ng cluster `xcpng154/155/156` is reachable independently
   from the old `nocpool-fc640` production cluster; do not relocate XOCE.
2. Present **new separate** Compellent storage only to the new pool, with the
   intended shared SR visible on all three hosts and supported multipathing
   fully tested. A successful iSCSI TCP session without a LUN is a STOP.
3. Verify read/write VLAN inventory, VLAN 1 native mapping, LACP Po154/155/156,
   dedicated iSCSI VLAN248/249, and each VM's guest NIC identity. Treat the
   broker SPAN NIC, NAC01 multihoming and NFS host as exceptional.
4. Verify VMware `Po151/Po152/Po153` member ports, vDS lag1, distributed
   port groups and iSCSI VLANs; current baseline includes MTU9000 on vmk0/1/2.
5. Validate source VM firmware, secure boot/TPM, guest agents, application
   quiescence and storage/CPU/memory capacity. Unknown or unverified fields
   block the migration planner.
6. Run and verify a fresh immutable backup, including a *separate*
   replica restore-verification for any required DR copy.
7. Restore a disposable pilot to a new name on an isolated VLAN, boot and
   check the application, then destroy only the disposable pilot.
8. Sign off an application-aware final-sync/fencing order. **Never boot
   target alongside a writable production source using same IP/MAC.**
9. Demonstrate rollback, including the path to re-enable original source
   without losing writes or causing duplicate AD/DNS/DHCP services.
10. Require operations, application owner and security signoffs before the
    migration is labeled production certified.

## Failure test matrix

- Primary repository unreachable, backup password invalid, partial pack copy
- Replica snapshot listed but data corrupted, missing or inaccessible
- Restore manifest mismatch, path traversal, stale verification flag
- Power loss during copy, full verification staging disk, network timeout
- Old VM still running / target power-on attempted concurrently
- Shared SR present on one host only, multipath degraded, LACP port down
- VLAN mapping missing, untagged SPAN NIC remapped incorrectly
- Wrong destination cluster, stale discovery, firmware and RAM mismatch
- Approval forged, same requester/approver, changed plan checksum
- Dry-run makes no hypervisor changes; all failed cutovers leave source intact

## Release decision

Do not tag as production-ready until the entire CI matrix, real data-plane
restore, live XCP-ng and VMware pilot, application recovery, SAN failover,
replica immutability and disaster recovery failback tests are recorded.
See `docs/PRODUCTION_ACCEPTANCE.md` and `docs/CLUSTER_MIGRATION.md`.
