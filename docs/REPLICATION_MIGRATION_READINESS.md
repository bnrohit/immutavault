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

## Cluster migration acceptance (public, vendor-neutral)

1. Ensure the new target cluster is reachable independently from the old
   production cluster; do not relocate the backup/migration control plane.
2. Present **new separate** SAN storage to the target pool, ensure a real
   shared SR is visible on all hosts, and test supported multipathing. A
   successful iSCSI session without a LUN is a STOP.
3. Verify switch trunking, link aggregation, dedicated storage networks,
   native VLAN semantics and each guest's NIC identity. Treat SPAN ports,
   NAC appliances and multihomed file servers as special workloads.
4. Validate VMware distributed switch LAGs and port groups, dedicated iSCSI
   storage networks and end-to-end MTU, according to the site's private baseline.
5. Validate firmware, secure boot/TPM, guest agents, application quiescence,
   hardware/CPU compatibility and destination resource capacity.
6. Verify a fresh immutable primary backup; independently restore a selected
   replica copy and verify its manifest when the DR copy is required.
7. Restore an isolated disposable pilot under a new name, boot and check the
   application, then clean up only the disposable test.
8. Sign off an application-consistent final synchronization and fencing order.
   **Never boot target beside a writable source with duplicate IP/MAC.**
9. Test rollback, retaining original production workloads until signoff.
10. Require separate operations, application owner and security acceptance.

Store **site-specific VLANs, IPs, hostnames, switch ports, serial numbers,
credentials, backup recovery IDs and SAN mappings in a private runbook**, never
in this public repository.

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
