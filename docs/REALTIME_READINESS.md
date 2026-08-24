# Real-time and Production Readiness

Immutavault v0.5.1 separates **correct recovery orchestration** from marketing claims about zero downtime. The current release can keep protected VMs running during supported snapshot/export workflows and can orchestrate DR promotion/failback, but it is not continuous block replication and does not provide an RPO of zero.

## What is exercised automatically

The release suite validates configuration, recovery policy, retention, adapters, fencing/quorum logic, VXLAN/OSPF command generation, installers, systemd units, package creation and security boundaries. GitHub CI additionally runs a disposable **real restic + rest-server data plane** over authenticated TLS: initialize repository, backup, repository check, restore, SHA-256 compare, deliberate network-writer delete attempt, and confirmation that the protected snapshot remains readable.

CI runs the Python control plane on Python 3.10, 3.11, 3.12, 3.13 and 3.14. The installer pins restic 0.19.1 by SHA-256 and capability-gates rest-server 0.14.0+.

## Transport reality

| Platform | Current v0.5.1 behavior | Production VM intentionally powered off? | Incremental/CDP? |
|---|---|---:|---:|
| VMware/vCenter | quiesced/crash-consistent snapshot -> powered-off temporary clone -> full OVF/VMDK export | No | No; VDDK/CBT is future work |
| Proxmox VE | `vzdump --mode snapshot` -> archive -> restic | No for supported snapshot backups | No; native PBS integration is future work |
| XCP-ng | `vm-snapshot` -> `snapshot-export-to-template` XVA -> restic | No | No; XO/CBT integration is future work |

These transports can produce application-consistent or crash-consistent points without a planned guest shutdown, but full exports can be large. Snapshot creation/removal may cause short hypervisor/guest pauses, and backup duration depends on VM size, storage, CPU and network throughput.

## DR reality

DR promotion is restore-before-boot. RTO therefore includes validating the off-site point, staging/restoring the VM payload, importing it into the DR hypervisor, route/gateway movement, boot ordering and application health checks. VXLAN/OSPF orchestration can preserve configured service IPs, but it does not make a full-image restore instantaneous.

For workloads needing seconds-level failover or near-zero data loss, use application clustering/database replication or native continuous/incremental replication in addition to Immutavault until VDDK/CBT, PBS and XO/CBT transports are fully integrated and certified.

## Production acceptance

No software-only test can certify a specific Cisco/Dell/Lenovo server, vCenter, Proxmox cluster, XCP-ng pool, S3 account, storage array, firewall and routed DR network. Before unattended failover, run `docs/PRODUCTION_ACCEPTANCE.md` against the actual environment and complete two controlled failover/failback drills with real fencing. Keep `auto_failover: false` until those drills pass.
