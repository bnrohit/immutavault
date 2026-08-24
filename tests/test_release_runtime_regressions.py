from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rest_server_archive_does_not_depend_on_preserved_execute_bit():
    text = (ROOT / "scripts/install_rest_server.sh").read_text(encoding="utf-8")
    assert "-perm -u+x" not in text
    assert 'install -o root -g root -m 0755 "$BIN" "$DEST"' in text
    assert 'check_rest_server.sh" "$DEST"' in text


def test_release_checker_falls_back_to_isolated_wheel_build():
    text = (ROOT / "scripts/release_check.sh").read_text(encoding="utf-8")
    assert "import setuptools.build_meta" in text
    assert "pip wheel . --no-deps --no-build-isolation" in text
    assert "pip wheel . --no-deps -w dist" in text
