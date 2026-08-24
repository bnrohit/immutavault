from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_rest_server.sh"


def _fake_server(tmp_path: Path, version: str, help_text: str) -> Path:
    p = tmp_path / "rest-server"
    p.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ ${1:-} == --version ]]; then printf '%s\\n' 'rest-server " + version + "'; exit 0; fi\n"
        "if [[ ${1:-} == --help ]]; then cat <<'EOF'\n" + help_text + "\nEOF\nexit 0\nfi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


REQUIRED_HELP = """
--append-only
--tls
--tls-cert string
--tls-key string
--tls-min-ver string one of (1.2|1.3)
--htpasswd-file string
""".strip()


def test_compat_checker_accepts_current_capabilities(tmp_path):
    fake = _fake_server(tmp_path, "0.14.0", REQUIRED_HELP)
    cp = subprocess.run([str(CHECKER), str(fake)], text=True, capture_output=True)
    assert cp.returncode == 0, cp.stderr
    assert "Compatible rest-server detected" in cp.stdout


def test_compat_checker_rejects_old_version_even_if_flags_exist(tmp_path):
    fake = _fake_server(tmp_path, "0.13.0", REQUIRED_HELP)
    cp = subprocess.run([str(CHECKER), str(fake)], text=True, capture_output=True)
    assert cp.returncode != 0
    assert "older than required 0.14.0" in cp.stderr


def test_compat_checker_rejects_missing_append_only(tmp_path):
    fake = _fake_server(tmp_path, "0.14.0", REQUIRED_HELP.replace("--append-only\n", ""))
    cp = subprocess.run([str(CHECKER), str(fake)], text=True, capture_output=True)
    assert cp.returncode != 0
    assert "--append-only" in cp.stderr


def test_compat_checker_rejects_missing_hardened_tls_selector(tmp_path):
    fake = _fake_server(tmp_path, "0.14.0", REQUIRED_HELP.replace("--tls-min-ver string one of (1.2|1.3)\n", ""))
    cp = subprocess.run([str(CHECKER), str(fake)], text=True, capture_output=True)
    assert cp.returncode != 0
    assert "--tls-min-ver" in cp.stderr
