from pathlib import Path


def test_start_limit_is_in_unit_section():
    root = Path(__file__).resolve().parents[1] / "systemd"
    for name in ("immutavault-portal.service", "immutavault-rest-server.service"):
        text = (root / name).read_text(encoding="utf-8")
        unit, service = text.split("[Service]", 1)
        assert "StartLimitIntervalSec=0" in unit
        assert "StartLimitIntervalSec=0" not in service
