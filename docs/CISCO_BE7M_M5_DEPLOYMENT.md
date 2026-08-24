# Cisco BE7M-M5-K9 Deployment Profile

The Cisco BE7M-M5-K9 is based on the UCS C240 M5SX platform. It is suitable for the Immutavault control plane when repurposed as a dedicated Linux backup appliance or when used for a lab/pilot VM.

## Recommended use

- Prefer **dedicated bare-metal Linux** for the immutable vault if the server will protect production hypervisors.
- Do not place the only backup vault as a VM on the same ESXi/vCenter estate it is intended to recover.
- The original BE7000M disk layout is capacity-limited for a large backup estate. Use external NFS storage, larger supported drives/storage shelves, or S3 replicas.
- Use the available 10GbE interfaces for backup/storage traffic where possible.
- Keep CIMC management on a separate management network.

## Suggested layout

```text
Cisco BE7M-M5-K9 / UCS C240 M5SX
  Linux OS
  Immutavault controller
  Immutavault recovery portal
  rest-server (append-only)
       |
       +--> local/external NFS primary repository
       +--> Wasabi / IDrive e2 / Backblaze B2 immutable S3 copy
       +--> Cloudflare R2 copy with native Bucket Lock rules
```

The CPU and memory are primarily responsible for orchestration, encryption, cataloging, and verification. Repository capacity and throughput are dominated by the attached storage, disk layout, and network bandwidth.
