"""Pydantic models for every structured answer expected from the LLM.

The JSON Schema sent to the model (response_format or inline in the prompt)
is generated from these models, so prompts and validation never drift apart.
"""
from __future__ import annotations

import json
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Decision = Literal["remove", "keep", "unsure", "needs_context"]
Confidence = Literal["high", "medium", "low"]


def _short(value: object, limit: int) -> str:
    return str(value or "")[:limit]


class Verdict(BaseModel):
    id: str
    decision: Decision
    confidence: Confidence = "low"
    reason: str = ""
    evidence: List[str] = Field(default_factory=list)

    @field_validator("decision", mode="before")
    @classmethod
    def _decision(cls, value: object) -> str:
        text = str(value or "").strip().lower().replace(" ", "_")
        return text if text in ("remove", "keep", "unsure", "needs_context") else "unsure"

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: object) -> str:
        text = str(value or "").strip().lower()
        return text if text in ("high", "medium", "low") else "low"

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, value: object) -> str:
        return _short(value, 700)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> List[str]:
        items = value if isinstance(value, list) else ([value] if value else [])
        return [_short(item, 300) for item in items][:6]


class VerdictBatch(BaseModel):
    verdicts: List[Verdict] = Field(default_factory=list)


class EntryPoint(BaseModel):
    path: str
    kind: str = "other"
    why: str = ""


class WiringMechanism(BaseModel):
    mechanism: str
    where: str = ""
    implication_for_dead_code: str = ""


class RuntimeItem(BaseModel):
    item: str
    why: str = ""


class ProtectedPattern(BaseModel):
    pattern: str
    why: str = ""


class ProjectContext(BaseModel):
    stack_summary: str = ""
    frameworks: List[str] = Field(default_factory=list)
    entry_points: List[EntryPoint] = Field(default_factory=list)
    wiring_mechanisms: List[WiringMechanism] = Field(default_factory=list)
    runtime_or_external: List[RuntimeItem] = Field(default_factory=list)
    never_remove: List[ProtectedPattern] = Field(default_factory=list)
    notes: str = ""


class DiscoveryItem(BaseModel):
    category: str = "leftover"
    path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    first_line_text: Optional[str] = None
    last_line_text: Optional[str] = None
    symbol: Optional[str] = None
    decision: Literal["remove", "unsure"] = "unsure"
    confidence: Confidence = "low"
    reason: str = ""
    evidence: List[str] = Field(default_factory=list)

    @field_validator("decision", mode="before")
    @classmethod
    def _decision(cls, value: object) -> str:
        return "remove" if str(value or "").strip().lower() == "remove" else "unsure"

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: object) -> str:
        text = str(value or "").strip().lower()
        return text if text in ("high", "medium", "low") else "low"

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> List[str]:
        items = value if isinstance(value, list) else ([value] if value else [])
        return [_short(item, 300) for item in items][:6]


class DiscoveryBatch(BaseModel):
    findings: List[DiscoveryItem] = Field(default_factory=list)


def schema_text(model: type) -> str:
    """Compact JSON Schema of a model, to embed in prompts."""
    return json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
