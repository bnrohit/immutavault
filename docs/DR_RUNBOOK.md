# Disaster recovery runbook

Immutavault DR is designed to move **recovered workloads and route ownership together**. Same-IP recovery is allowed only for explicitly configured recovery VLANs and only after primary-site fencing prevents split brain.

## Required topology

- Primary and DR compute platforms.
- Verified off-site Immutavault replica accessible from the DR control plane.
- A Linux/FRR DR gateway at each site (or equivalent tested gateway role).
- Routed reachability between VTEP addresses.
- VLAN trunk from each gateway toward its local recovery compute/network.
- OSPF adjacency from each gateway into the routed network that should learn recovered prefixes.
- Unique OSPF router IDs.
- Fencing mechanism for unattended failover.
- A controller that survives primary-site loss.

Install a gateway only after reviewing interfaces:

```bash
sudo ./scripts/install_dr_gateway.sh
```

The gateway installer installs prerequisites only; it does not silently create production routes/VLANs.

## Commissioning

Keep automatic failover disabled:

```yaml
disaster_recovery:
  enabled: true
  auto_failover: false
  primary_site: primary
  dr_site: offshore-dr
  control_plane_site: offshore-dr
```

Plan/inspect:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-plan
immutavault --config /etc/immutavault/immutavault.yml dr-preflight
immutavault --config /etc/immutavault/immutavault.yml dr-network plan --site offshore-dr
```

Prepare the inactive DR network only after the generated plan is reviewed:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-network prepare --site offshore-dr --execute
```

Synchronize recovery points:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-sync
```

## Controlled failover

1. Declare/confirm the incident.
2. Enable maintenance mode while humans investigate if automatic monitoring is present:
   `immutavault ... dr-maintenance on --actor operator`.
3. Confirm the primary site cannot safely continue.
4. Run `dr-preflight` while primary is still available if possible.
5. Fence/isolate the primary compute/network. Do not rely on one ICMP result.
6. Preview `dr-promote`.
7. Execute only with explicit fenced confirmation.

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-promote \
  --execute --confirm-primary-fenced
```

The orchestrator verifies the DR replica/target/gateway before fencing when it performs configured command fencing, restores VMs powered off, activates the DR gateway/OSPF route, then powers workloads on in configured boot order and runs health checks.

If a workload health check fails, treat the failover as incomplete and follow the logged failure state. Do not manually start duplicate primary copies.

## Automatic failover

Do not enable until controlled failover/failback drills pass.

Automatic mode requires:

- `control_plane_site` not equal to primary site.
- multiple primary probes as appropriate.
- `primary_failure_quorum`.
- `failure_threshold` consecutive failed evaluations.
- `check_interval_seconds`.
- a command fence and **separate verification command** supplied by environment variables.
- a tested maintenance lock path.

The one-minute systemd watcher respects larger configured check intervals; waking once per minute does not increment the failure counter until the configured interval is due.

## Failback

Do not simply restart primary VMs when the site returns. That creates duplicate same-IP services and loses changes made in DR.

1. Repair primary and validate storage/network/hypervisor.
2. Keep primary application copies isolated/off.
3. Preview:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-failback
```

4. Execute:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-failback \
  --execute --confirm-primary-isolated
```

Immutavault takes final backups of active DR workloads, restores those current states to primary powered off, withdraws DR route ownership/powers DR copies off, activates primary route ownership, then boots primary copies and health-checks them.

## Same-IP safety

For a recovery VLAN such as `10.14.48.0/21` with gateway `10.14.48.1/21`, **only the active site may own `10.14.48.1` and advertise the subnet**. The control plane must never activate both sides. Fencing and route checks are mandatory acceptance tests.

Do not send raw VXLAN across an untrusted Internet path. Use a controlled private WAN or an encrypted tunnel/underlay and validate MTU/fragmentation.
