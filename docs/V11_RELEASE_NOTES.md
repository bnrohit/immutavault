# Immutavault v1.1.0 release notes

v1.1.0 unifies guided setup, policy protection and recovery in the enterprise portal while preserving the fail-closed immutable/V2V/tenant security model from v1.0.1.

## Added

- Unified **Overview / Protect / Recovery / Setup & Manage** portal.
- Privilege-separated management broker authenticated with Linux `SO_PEERCRED`.
- Named protection policies with exact VM selections, daily/weekly/hourly/manual schedules, immutable days, explicit replica targets and verification.
- Direct NFS and SMB 3.1.1 onboarding below `/srv/immutavault/storage` with temporary mount tests and generated systemd mount units.
- Isolated **Run DR Test** workflow using the normal restore/four-eyes path; verified/non-suspicious source required, target network must be live-validated and allow-listed, every NIC is isolated before boot, and cleanup is required by default.
- Pinned release bootstrap installer with optional independent source-archive SHA-256.
- Appliance builder for checksum-verified Ubuntu 24.04 bases producing QCOW2, VMware OVA and XCP-ng VHD import artifacts plus `SHA256SUMS`.
- v1.1 example configuration and management/appliance runbooks.

## Security retained

- Append-only authenticated TLS repository data plane.
- Entra/OIDC MFA, tenant scoping and cross-tenant restore prohibition.
- Portal `NoNewPrivileges=true`, `PrivateDevices=true`, empty capability set.
- Root-only FLR and management brokers; policy workers stay unprivileged.
- Certified VMware-to-Proxmox V2V remains fail closed; Secure Boot/vTPM restrictions remain.
- Broadcom VDDK is not bundled.
- Generic install still does not enable automatic DR promotion.

## Artifact boundary

The builder intentionally emits an explicit `.vhd` for XCP-ng disk import and does not fabricate or rename another image as `.xva`. A native XVA appliance remains gated on a separately validated XVA packager/import test.
