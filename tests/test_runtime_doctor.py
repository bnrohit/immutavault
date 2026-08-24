from dataclasses import replace

from immutavault.config import load_config
from immutavault.engine import BackupEngine
from immutavault.runner import CommandResult


def test_doctor_rejects_old_runtime_restic(monkeypatch, tmp_path):
    base = load_config("config/immutavault.example.yml")
    cfg = replace(
        base,
        runtime=replace(base.runtime, state_db=str(tmp_path / "state.db")),
        repository=replace(base.repository, staging_path=str(tmp_path / "staging")),
        platforms=[],
        replicas=[],
    )
    monkeypatch.setattr("immutavault.engine.shutil.which", lambda name: "/usr/bin/restic" if name == "restic" else None)
    monkeypatch.setattr("immutavault.engine.run", lambda command, **kwargs: CommandResult(command, 0, "restic 0.18.1 compiled with go", ""))
    result = BackupEngine(cfg).doctor()
    assert any("older than the tested minimum 0.19.1" in p for p in result["repository"])
