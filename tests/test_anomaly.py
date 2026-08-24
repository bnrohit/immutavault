from immutavault.anomaly import detect_backup_anomaly


def test_anomaly_high_churn():
    suspicious, reason = detect_backup_anomaly(
        previous_bytes=1000,
        current_bytes=1000,
        data_added=900,
        data_added_ratio_threshold=0.70,
        size_change_ratio_threshold=0.50,
    )
    assert suspicious
    assert "churn" in (reason or "")


def test_anomaly_normal_delta():
    suspicious, reason = detect_backup_anomaly(
        previous_bytes=1000,
        current_bytes=1020,
        data_added=50,
        data_added_ratio_threshold=0.70,
        size_change_ratio_threshold=0.50,
    )
    assert not suspicious
    assert reason is None


def test_first_backup_establishes_baseline():
    suspicious, reason = detect_backup_anomaly(
        previous_bytes=None,
        current_bytes=1000,
        data_added=1000,
        data_added_ratio_threshold=0.70,
        size_change_ratio_threshold=0.50,
    )
    assert not suspicious
    assert reason is None
