from __future__ import annotations

import re


def safe_component(value: str, *, fallback: str = "unnamed") -> str:
    value = value.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned[:160] or fallback
