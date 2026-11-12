"""Prompt templates (English). Placeholders are {{UPPER_CASE}} and must all be filled."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

PROMPTS_DIR = Path(__file__).resolve().parent
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def render(name: str, values: Dict[str, str]) -> str:
    """Fill a template in a single pass, so values that contain `{{...}}` (Mustache,
    Angular or JSP code inside an evidence pack) are inserted verbatim."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    missing = sorted({m.group(0) for m in PLACEHOLDER.finditer(text) if m.group(0)[2:-2] not in values})
    if missing:
        raise RuntimeError(f"Marcadores sin sustituir en el prompt {name}: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(0)[2:-2]], text)
