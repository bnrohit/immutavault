from immutavault.integrity import build_manifest, verify_manifest


def test_manifest_detects_change(tmp_path):
    root = tmp_path / "payload"; root.mkdir()
    (root / "a.txt").write_text("hello")
    _, digest = build_manifest(root)
    ok, errors = verify_manifest(root, expected_digest=digest)
    assert ok and not errors
    (root / "a.txt").write_text("changed")
    ok, errors = verify_manifest(root)
    assert not ok
    assert errors


def test_manifest_digest_detects_manifest_tamper(tmp_path):
    root = tmp_path / "payload"; root.mkdir()
    (root / "a.txt").write_text("hello")
    _, digest = build_manifest(root)
    manifest = root / ".immutavault-manifest.json"
    text = manifest.read_text()
    manifest.write_text(text.replace('"file_count": 1', '"file_count": 999'))
    ok, errors = verify_manifest(root, expected_digest=digest)
    assert not ok
    assert "manifest digest mismatch" in errors
