# Immutavault vs. enterprise backup platforms

Immutavault is designed to become a strong open recovery vault, but it must not claim blanket superiority over mature commercial products before the same workloads, failure modes, performance, and restore scenarios have been tested.

## Implemented in v0.5

| Area | Immutavault v0.5 |
|---|---|
| Vendor-neutral Linux vault | Yes; x86_64/amd64/aarch64 design, no Dell/Cisco/Lenovo lock-in |
| VMware/vCenter | Inventory, snapshot-assisted hot-clone OVF backup, safe new-VM OVF restore |
| Proxmox VE | Inventory, snapshot `vzdump`, safe `qmrestore`/`pct restore` |
| XCP-ng | Inventory, snapshot-export XVA backup, safe XVA import |
| Encrypted deduplicated repository | Yes, restic |
| Append-only writer | Yes, rest-server append-only trust zone |
| Root-only expiration/prune | Yes |
| GFS retention | Yes |
| Recovery-point immutable hold | Yes; extension only |
| Self-service point selection | Yes, scoped HTTPS portal/API |
| Four-eyes restore | Yes |
| Recovery-point verification | Yes, full staged restore + manifest validation |
| Anomaly protection | Yes, churn/size anomaly detection + longer preservation |
| Recovery readiness score | Yes |
| Tamper-evident audit chain | Yes, SHA-256 linked events |
| Replica copy | Yes, restic replica targets |
| Existing-VM overwrite protection | Yes, automatic overwrite refused |
| Restore target preflight | Yes |

## Still required before claiming full enterprise parity

- VMware VDDK/CBT production transport and version certification.
- Native Proxmox Backup Server chunk/incremental integration.
- Native Xen Orchestra delta/incremental integration.
- Instant VM boot directly from backup storage.
- Application-aware quiescing and item-level recovery for AD, SQL, Exchange, Oracle, PostgreSQL and similar workloads.
- File-level guest restore without full VM export staging.
- Continuous data protection / journal-based sub-backup-interval RPO.
- Tested cross-hypervisor conversion with firmware, disk-controller, NIC and guest-tools handling.
- Object-lock cloud backends with independently administered credentials.
- Large-estate scheduling, concurrency/QoS, proxy/worker scaling, HA control plane, metrics/SIEM integrations and enterprise support lifecycle.

These are engineering targets, not hidden claims. The safe core blocks workflows that have not been proven rather than pretending all hypervisor/version combinations are interchangeable.
