from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repository_installer_is_idempotent_for_required_secrets():
    text = (ROOT / "scripts/install_repository.sh").read_text(encoding="utf-8")
    for name in ("RESTIC_PASSWORD", "REST_SERVER_USER", "REST_SERVER_PASSWORD"):
        assert f"grep -q '^{name}='" in text
    assert "IMMUTAVAULT_REPO_ROOT=$ROOT" in text


def test_custom_repository_root_is_used_by_service_and_installer():
    service = (ROOT / "systemd/immutavault-rest-server.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install_repository.sh").read_text(encoding="utf-8")
    assert "Environment=IMMUTAVAULT_REPO_ROOT=/srv/immutavault" in service
    assert "${IMMUTAVAULT_REPO_ROOT}/repository" in service
    assert "${IMMUTAVAULT_REPO_ROOT}/.htpasswd" in service
    assert "repo['local_path'] = f'{root}/repository'" in installer
    assert "runtime['restore_staging_path'] = f'{root}/restore-staging'" in installer
    assert "runtime['verify_staging_path'] = f'{root}/verify-staging'" in installer


def test_installer_does_not_auto_enable_dr_failover():
    text = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "DR timers are deliberately NOT enabled" in text
    assert "systemctl enable --now immutavault-dr-watch.timer" not in text


def test_runtime_install_and_upgrade_do_not_require_pip_upgrade():
    for rel in ("scripts/install_controller.sh", "scripts/upgrade.sh"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "pip install --upgrade pip" not in text
        assert "--no-deps --no-build-isolation" in text


def test_systemd_controller_services_have_no_hardcoded_repository_write_path():
    for path in (ROOT / "systemd").glob("*.service"):
        text = path.read_text(encoding="utf-8")
        if path.name == "immutavault-rest-server.service":
            continue
        assert "ReadWritePaths=/srv/immutavault" not in text


def test_verified_rest_server_installer_is_pinned_and_checksummed():
    text = (ROOT / "scripts/install_rest_server.sh").read_text(encoding="utf-8")
    assert 'VERSION="${REST_SERVER_VERSION:-0.14.0}"' in text
    assert "sha256sum --check --status" in text
    assert "4c9c95bc079a0334e81fad379b19dc5c3353c71c2c88d652cafce2081c2b1c66" in text
    assert "cef139cbe8b27b16bda731d17f093b0aa466b8c60b136c12d78b6f2bff3daf22" in text


def test_all_or_repository_role_does_not_claim_completion_without_rest_server():
    text = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "./scripts/install_rest_server.sh" in text
    assert "exit 3" in text


def test_standby_promotion_is_single_writer_and_integrity_checked():
    text = (ROOT / "scripts/promote_standby.sh").read_text(encoding="utf-8")
    assert "PRAGMA integrity_check" in text
    assert "audit-verify" in text
    assert "systemctl stop immutavault-portal.service" in text
    assert "immutavault-backup.timer" in text
    assert "No backup, retention, verify, or DR-sync jobs were enabled automatically" in text
    assert "--activate-dr-watch" in text


def test_live_data_plane_acceptance_tests_append_only_and_restore_digest():
    text = (ROOT / "scripts/data_plane_acceptance.sh").read_text(encoding="utf-8")
    assert 'restic backup "$SRC"' in text
    assert 'restic restore "$SNAP"' in text
    assert 'sha256sum "$RESTORED"' in text
    assert 'restic forget "$SNAP"' in text
    assert 'SECURITY FAILURE: append-only network writer successfully forgot/deleted a snapshot' in text
    assert 'RESTIC_REPOSITORY="$LOCAL_REPO"' in text


def test_appliance_text_matches_verified_rest_server_install_path():
    text = (ROOT / "scripts/install_appliance.sh").read_text(encoding="utf-8")
    assert "does NOT download rest-server binaries" not in text
    assert "pinned upstream rest-server binary only after SHA-256 verification" in text
    assert "check_rest_server.sh" in text


def test_repository_rejects_incompatible_rest_server_capabilities():
    checker = (ROOT / "scripts/check_rest_server.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install_repository.sh").read_text(encoding="utf-8")
    top = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    for flag in ("--append-only", "--tls", "--tls-cert", "--tls-key", "--tls-min-ver", "--htpasswd-file"):
        assert flag in checker
    assert 'MIN_VERSION="${IMMUTAVAULT_MIN_REST_SERVER_VERSION:-0.14.0}"' in checker
    assert 'check_rest_server.sh" "$(command -v rest-server)"' in installer
    assert "Existing rest-server is incompatible" in top


def test_verified_rest_server_is_rechecked_after_install():
    text = (ROOT / "scripts/install_rest_server.sh").read_text(encoding="utf-8")
    assert 'check_rest_server.sh" "$DEST"' in text


def test_quickstart_does_not_reinitialize_repository_after_all_role_install():
    text = (ROOT / "docs/QUICKSTART.md").read_text(encoding="utf-8")
    assert "sudo ./scripts/install_repository.sh /srv/immutavault" not in text
    assert "pinned/SHA-verified upstream `rest-server`" in text


def test_preflight_rejects_incompatible_installed_rest_server():
    text = (ROOT / "scripts/preflight.sh").read_text(encoding="utf-8")
    assert "check_rest_server.sh" in text
    assert "installed but incompatible" in text


def test_runtime_install_builds_wheel_outside_versioned_venv():
    controller = (ROOT / "scripts/install_controller.sh").read_text(encoding="utf-8")
    upgrade = (ROOT / "scripts/upgrade.sh").read_text(encoding="utf-8")
    for text in (controller, upgrade):
        assert "python3 -m pip wheel . --no-deps --no-build-isolation" in text
        assert 'pip" install --no-deps --no-index "$WHEEL"' in text
    assert '"$TMP/bin/pip" install --no-deps --no-build-isolation .' not in controller
    assert '"$TARGET/bin/pip" install --no-deps --no-build-isolation .' not in upgrade


def test_first_install_virtualenv_is_not_relocated_after_creation():
    text = (ROOT / "scripts/install_controller.sh").read_text(encoding="utf-8")
    assert 'python3 -m venv --system-site-packages "$TARGET"' in text
    assert 'mv "$TMP" "$TARGET"' not in text
    assert 'ln -sfn "$TARGET" "${CURRENT}.new"' in text
