from __future__ import annotations


def detect_backup_anomaly(
    *, previous_bytes: int | None, current_bytes: int, data_added: int,
    data_added_ratio_threshold: float, size_change_ratio_threshold: float,
) -> tuple[bool, str | None]:
    reasons: list[str] = []
    # The first full backup establishes the baseline and is expected to add nearly 100%
    # new data. Churn becomes meaningful only when a prior recovery point exists.
    if previous_bytes is not None and previous_bytes > 0 and current_bytes > 0 and data_added > 0:
        ratio = data_added / current_bytes
        if ratio >= data_added_ratio_threshold:
            reasons.append(f"high repository churn ({ratio:.0%} new data)")
    if previous_bytes is not None and previous_bytes > 0:
        delta = abs(current_bytes - previous_bytes) / previous_bytes
        if delta >= size_change_ratio_threshold:
            reasons.append(f"unusual backup size change ({delta:.0%})")
    return bool(reasons), "; ".join(reasons) if reasons else None
