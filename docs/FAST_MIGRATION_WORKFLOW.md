# Fast migration workflow — batching without unsafe shortcuts

**Development feature:** Immutavault v1.2 branch, draft PR #28.
Not a production-certified warm migration or automated cutover tool.

The time-saving changes are intended to remove repetitive CLI requests and
operator scheduling while keeping existing four-eyes approvals and critical
VM safeguards.

## Two independent ways to save time

1. **Less planning effort:** `migration-optimize` groups already reviewed
   VM manifests into resource-safe staging waves. Each wave reserves destination
   hosts and storage tokens. Default: 2 total jobs, 1 per host, **1 per shared
   storage repository**. It never assumes doubling jobs doubles bandwidth.
2. **Less typing:** `migration-request-batch` creates up to 25 separate
   pending restore requests per invocation, with prevalidation, existing-request
   deduplication and safe retries. Nothing is approved automatically and
   no VM is imported, started, shut down or replaced by this command.

Data transfers still use the existing backup/export/restore path. There is
**no** CBT/XVA resumable block-delta cluster migration or proven parallel
import execution yet, so major reductions in elapsed copy time are not claimed.

## Prerequisites

Use an expendable, isolated test VM first. Configure separate source and
destination platforms. Verify exact network and storage mappings, target SR
visibility and multipath, restore points, guest consistency and all other
`migration-plan` gates. Store inventory, recovery IDs, approval details,
network and SAN maps privately — not in the public Git repository.

All commands below operate on the **development branch installed in an
isolated test controller**. Do not install the branch directly over a working
production backup controller.

```bash
immutavault --config /etc/immutavault/immutavault.yml \
  migration-discover --platform old-xcp --output source.json
immutavault --config /etc/immutavault/immutavault.yml \
  migration-discover --platform new-xcp --output destination.json
# Populate selection.json privately using config/examples/migration-selection.example.json
immutavault --config /etc/immutavault/immutavault.yml \
  migration-plan --source source.json --destination destination.json \
  --selection selection.json --output plan.json
immutavault --config /etc/immutavault/immutavault.yml \
  migration-summary --plan plan.json
```

## Step 1 — produce bandwidth and resource-safe waves

```bash
immutavault --config /etc/immutavault/immutavault.yml \
  migration-optimize --plan plan.json \
  --effective-mib-per-second 100 \
  --max-parallel 2 --max-per-host 1 --max-per-storage 1 \
  --stage-overhead-minutes 15 --output migration-waves.json
```

The effective MiB/s setting must reflect an **actually measured sustained
aggregate** path from vault to target, not an assumed link line rate.
Provisioned disk bytes are used as pessimistic illustrative input, not the
actual export, deduplication or physical transfer size. The wave estimate
excludes export, backup generation, warm final delta, application checks,
transfer retries, memory pressure and storage bottlenecks.

If two VMs land on the same storage SR, they are serialized by default to
avoid saturating the same SAN. Setting `--max-per-storage 2` can stage them in
the same *advisory wave* after load testing; it does **not** itself launch
concurrent writes.

For dependency order or business priority, use optional private JSON files:

```json
{
  "vm-a": 3,
  "vm-b": 1
}
```

as `--priorities /private/priority.json` (3 highest), and

```json
{
  "vm-b": ["vm-a"]
}
```

as `--dependencies /private/dependencies.json`. These are exact source VM
IDs, not VM names. Cycles, unknown VMs, tampered plans and unready workloads
fail closed. Dependencies enforce *different-wave ordering* and are not a
substitute for application-specific shutdown/startup sequencing.

## Step 2 — request multiple VMs once, but approve separately

Dry run, without database or hypervisor writes:

```bash
immutavault --config /etc/immutavault/immutavault.yml \
  migration-request-batch \
  --source source.json --destination destination.json \
  --selection selection.json --plan plan.json \
  --requester operator --max-new-requests 25
```

Only when the plan is reviewed and source recovery points verified, add
`--execute-requests`. This creates **individual** pending restore requests
for XCP-ng -> XCP-ng. The entire new batch is checked for clean verified
recovery points before creating requests. The command deduplicates requests
by exact target, backup snapshot and plan SHA-256. If a request creation
fails midway, the earlier requests persist; rerunning skips them rather
than automatically retrying existing requests with different approval state.

Every request still requires its own approval by a different person. Follow
`docs/CLUSTER_MIGRATION.md` to review and stage a **powered-off new target**.
The scheduling output is advisory; there is no automatic wave executor,
parallel-import engine, source power-off, final incremental sync, destination
boot or production cutover.

## Engineering roadmap for real elapsed-time reduction

- Build certified source-provider changed-block tracking / export where
  supported (VMware CBT/VDDK, appropriate XCP-ng/XO mechanisms).
- Validate resumable transfers, checksums and idempotent partial transfers.
- Automatically track source data-change rates and run safe pre-copy passes.
- Benchmark the **entire** source->vault->target path including SAN and
  crypto overhead, and use those metrics for ETA and job concurrency limits.
- Add approved application-quiesced final sync, verified source fencing,
  isolated test power-on, and reversible cutover with split-brain defense.
- Provide service-specific rollback and on-prem failure-injection integration
  tests before exposing automatic production actions.

Nothing in this development module claims live migration, zero downtime,
zero data loss, or percentage time improvements without measurements.
