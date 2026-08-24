# Guided Setup Console

Immutavault v0.6 adds a browser-based setup console for operators who should not need to edit YAML, understand restic syntax, or manually write VXLAN/OSPF commands.

## What the wizard does

The wizard is split into five safe steps:

1. **Add a hypervisor** — VMware/vCenter, Proxmox VE, or XCP-ng. Enter the connection details and click **Test + discover**.
2. **Choose VMs to protect** — Immutavault retrieves the inventory and lets the operator check only the VMs that should be backed up. Exact VM names are saved; the wizard does not silently use a wildcard after selection.
3. **Add storage or cloud** — configure/test S3-compatible storage, a mounted NFS/SMB/NAS path, or a second Immutavault vault. Provider credentials are stored in the protected environment file, not in YAML. The wizard can initialize the repository and provider immutability where supported.
4. **Configure a DR site** — enter the two Linux DR gateways, VTEP addresses, VLAN/VNI, subnet, gateway, and select a source + same-family DR hypervisor. Immutavault maps the selected protected VMs and generates the DR plan. Network preparation requires typed confirmation.
5. **Test and start** — run `doctor`, a backup dry-run, the first real backup, and finally enable the normal backup/health/verification schedules.

Automatic DR failover is **never enabled by the setup wizard**.

## Start the wizard

Install the full appliance first:

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
git checkout v0.6.0
sudo ./scripts/preflight.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
```

Then start guided setup:

```bash
sudo ./scripts/launch_setup_console.sh
```

The launcher uses the portal TLS certificate by default and listens on TCP/8788. It prints a one-time setup token. Open:

```text
https://<immutavault-server>:8788/
```

Paste the one-time token into the first box.

To bind only to localhost:

```bash
sudo IMMUTAVAULT_SETUP_LISTEN=127.0.0.1 ./scripts/launch_setup_console.sh
```

The setup server refuses non-loopback plaintext operation.

## Step 1 — hypervisors

### VMware / vCenter

Enter:

- Friendly name, e.g. `vc-primary`
- vCenter SDK endpoint, e.g. `https://vcenter.example.local/sdk`
- service-account username and password
- optional datacenter, datastore, network, and resource-pool placement

Click **Test + discover**. Only save the platform after the test succeeds.

VMware credentials are converted into platform-specific environment references such as:

```text
IMMUTAVAULT_VC_PRIMARY_USERNAME
IMMUTAVAULT_VC_PRIMARY_PASSWORD
```

The values do not go into `immutavault.yml`.

### Proxmox / XCP-ng

Enter the hostname, SSH user, and private-key path. Each platform receives its own key reference, so the primary and DR sites do not need to share an SSH key.

The host key must already be present in the Immutavault host's `known_hosts`; SSH intentionally uses `StrictHostKeyChecking=yes`.

## Step 2 — select VMs

Choose the saved hypervisor and click **Discover VMs**. Check the VMs to protect and click **Save selected VMs**.

This changes the backup scope from `*` to the exact checked names. Reopening/saving the hypervisor later preserves the selected VM scope.

For a first deployment, select one disposable/test VM. Expand the scope only after a real backup and isolated restore succeed.

## Step 3 — storage/cloud

Supported wizard targets include:

- Wasabi
- IDrive e2
- Backblaze B2
- Cloudflare R2
- AWS S3
- MinIO / Ceph / custom S3-compatible endpoints
- mounted NFS/SMB/TrueNAS/Dell storage
- a second Immutavault `rest:` vault

For cloud providers, use the endpoint, region, bucket and credentials from the actual provider account. The wizard deliberately does not guess account-specific endpoints.

For NFS/SMB, mount the share in Linux first; then enter the mounted path in the wizard.

Use **Test storage** before **Save storage**, then **Initialize storage**. When provider immutability is enabled, initialization also validates/initializes the supported lock mechanism.

## Step 4 — disaster recovery

The wizard needs:

- primary and DR site names
- a previously configured DR replica
- primary and DR Linux gateway hostnames
- VTEP IP address at each gateway
- routed underlay interface
- hypervisor-facing VLAN trunk interface
- recovery VLAN ID
- VXLAN VNI
- recovery subnet
- same gateway CIDR used by the protected VMs
- optional DR-gateway SSH key path
- optional OSPF MD5 key (must match the upstream OSPF router if used)
- source hypervisor and same-family DR hypervisor

The workflow is:

```text
Save DR site/network
        ↓
Map selected VMs to DR
        ↓
Plan + preflight
        ↓
review generated commands
        ↓
Prepare DR network
```

`Prepare DR network` requires typing:

```text
APPLY DR NETWORK
```

Preparation builds the configured bridge/VLAN/VXLAN and FRR/OSPF side of the dedicated DR gateway. It **does not** claim the production gateway IP and **does not** promote VMs.

The routed underlay must already provide IP reachability between the VTEPs, and firewalls must permit the selected overlay/OSPF design. If the FRR gateway peers with an upstream router, that upstream router still needs the matching OSPF configuration. Immutavault does not guess or modify arbitrary third-party core/router configurations.

Cross-hypervisor automatic DR is blocked. For example, a VMware source must map to a VMware DR target until a separately certified V2V engine exists.

## Step 5 — test and start

Run in this order:

1. **Health check**
2. **Backup dry-run**
3. **First real backup**
4. Verify the recovery point
5. Perform one isolated restore and boot the restored VM
6. Only then click **Enable normal schedules**

The schedule button enables normal repository/portal/backup/state-backup/health/retention/verify services only. It does not enable `immutavault-dr-watch.timer`.

## DR promotion is intentionally separate

The setup console only builds and validates the DR configuration. Actual promotion stays in the guarded DR runbook because duplicate same-IP production VMs are dangerous.

Before promotion, the primary must be fenced/isolated. Use:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-plan
immutavault --config /etc/immutavault/immutavault.yml dr-preflight
immutavault --config /etc/immutavault/immutavault.yml dr-promote
```

After validating the plan and physically/logically fencing the primary:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-promote \
  --execute --confirm-primary-fenced
```

See `DR_RUNBOOK.md` before any production DR test.

## Stop the setup console

The setup console is an administrative first-run tool, not a service that should be exposed permanently. When configuration is complete, stop the foreground process with `Ctrl+C`. The normal recovery portal remains the day-to-day user interface on its configured HTTPS port.
