# Compatibility Strategy

Immutavault is hardware-vendor neutral at the backup-vault layer. Dell PowerEdge, Cisco UCS, Lenovo ThinkSystem and other standard servers are suitable when they run a supported Linux distribution and expose reliable local/block storage.

## Controller/vault OS

Target: modern 64-bit Linux with Python 3.10+, restic, rest-server, OpenSSH and systemd. Ubuntu/Debian and RHEL/Rocky-like package managers are handled by the appliance installer.

## Hypervisors

- VMware/vCenter: `govc` fallback uses public vSphere APIs available to govmomi. Exact vCenter/ESXi version compatibility follows the installed govc build and must be tested.
- Proxmox VE: fallback uses `pvesh`, `vzdump`, `qmrestore`, and `pct restore` over SSH. Exact CLI options vary across major versions and must be tested.
- XCP-ng: fallback uses `xe` export/import over SSH. Xen Orchestra REST/delta transport is the preferred future high-efficiency path.

## Why there is no “all versions” checkbox

A backup product should not claim every old/new hypervisor release without testing vendor API behavior. `immutavault doctor` is intentionally a live compatibility preflight, not a static marketing claim.
