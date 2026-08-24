# Compatibility Strategy

Immutavault is hardware-vendor neutral at the backup-vault layer. Dell PowerEdge, Cisco UCS, Lenovo ThinkSystem and other standard servers are suitable when they run a supported Linux distribution and expose reliable local/block storage.

## Controller/vault OS

Target: modern 64-bit Linux with Python 3.10+ (CI covers 3.10 through 3.14), pinned/verified restic 0.19.1, capability-gated rest-server 0.14.0+, OpenSSH and systemd. Ubuntu/Debian and RHEL/Rocky-like package managers are handled by the appliance installer.

## Hypervisors

- VMware/vCenter: the current govmomi/govc project supports vSphere 7.0 and higher. Immutavault still runs `doctor` and a disposable-VM acceptance test because privileges, distributed networks, guest tools and exact patch levels vary. Restore supports an explicit `network` mapping for DR vCenters and `options_json` for complex OVF mappings.
- Proxmox VE: fallback uses `pvesh`, `vzdump`, `qmrestore`, and `pct restore` over SSH. Exact CLI options vary across major versions and must be tested.
- XCP-ng: the direct path uses `xe vm-snapshot` + `snapshot-export-to-template`; restore imports the XVA template, instantiates a new VM with `vm-install`, then removes the temporary imported template. Xen Orchestra/CBT remains the preferred future high-efficiency transport.

## Why there is no “all versions” checkbox

A backup product should not claim every old/new hypervisor release without testing vendor API behavior. `immutavault doctor` is intentionally a live compatibility preflight, not a static marketing claim.
