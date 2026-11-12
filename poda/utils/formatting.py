"""Small formatting helpers shared by the UI and the reports."""
from __future__ import annotations

from typing import Optional


def fmt_secs(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def fmt_int(value: Optional[float]) -> str:
    if value is None:
        return "n/d"
    return f"{int(value):,}".replace(",", ".")


def fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "n/d"
    return f"{value:.4f} USD"
