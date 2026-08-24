# 15-minute lab quick start

This is for a non-production lab. Production requires a restore test and the full preflight in `INSTALLATION.md`.

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
sudo ./scripts/preflight.sh
sudo ./scripts/install.sh --role all
```

The `all` role installs the pinned/SHA-verified upstream `rest-server` when needed and initializes the repository. If an existing daemon is too old or lacks append-only/hardened TLS capabilities, installation fails closed or replaces it with the verified pinned release when downloads are allowed. Then configure:

```bash
sudo editor /etc/immutavault/immutavault.yml
sudo editor /etc/immutavault/immutavault.env
```

Enable exactly one test hypervisor and restrict `include:` to one disposable/test VM.

```bash
sudo -u immutavault bash -c 'set -a; source /etc/immutavault/immutavault.env; set +a; immutavault --config /etc/immutavault/immutavault.yml doctor'
sudo -u immutavault bash -c 'set -a; source /etc/immutavault/immutavault.env; set +a; immutavault --config /etc/immutavault/immutavault.yml inventory'
sudo -u immutavault bash -c 'set -a; source /etc/immutavault/immutavault.env; set +a; immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run'
sudo -u immutavault bash -c 'set -a; source /etc/immutavault/immutavault.env; set +a; immutavault --config /etc/immutavault/immutavault.yml backup --all'
```

Then browse the HTTPS recovery portal on port 8787, use a configured bearer token, select the new recovery point, verify it, and perform an isolated restore.
