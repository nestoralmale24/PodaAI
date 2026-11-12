"""Estimated cost of GitHub Copilot usage.

Since 1 June 2026 GitHub Copilot bills usage in GitHub AI Credits (1 credit = 0.01 USD), by tokens at
each model's rate: input, cached input and output. This module copies the rates GitHub publishes so a
run can show what it consumed. They change often: update ``PRICES`` and ``PRICES_AS_OF`` from
``PRICES_URL`` when GitHub changes them.

Not covered: annual Copilot Pro/Pro+ subscriptions, which still pay premium requests; cache writes
(Copilot does not report them, so they count as plain input); models GitHub does not list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

PRICES_AS_OF = "2026-11-12"
PRICES_URL = "https://docs.github.com/copilot/reference/copilot-billing/models-and-pricing"
CREDIT_USD = 0.01


@dataclass(frozen=True)
class Rate:
    """USD per million tokens."""
    input: float
    cached: float
    output: float


@dataclass(frozen=True)
class ModelPrice:
    base: Rate
    long_from: Optional[int] = None     # input tokens of a call above which `long` applies
    long: Optional[Rate] = None
    until: Optional[str] = None          # promotional price valid until this day (ISO); then `after`
    after: Optional[Rate] = None

    def rate(self, prompt_tokens: int, today: Optional[date] = None) -> Rate:
        if self.long_from is not None and self.long is not None and prompt_tokens > self.long_from:
            return self.long
        if self.until and self.after and (today or date.today()) >= date.fromisoformat(self.until):
            return self.after
        return self.base


def _tiers(base: Rate, long_from: int, long: Rate) -> ModelPrice:
    return ModelPrice(base, long_from=long_from, long=long)


_GROK = _tiers(Rate(2.00, 0.50, 6.00), 200_000, Rate(4.00, 1.00, 12.00))

# Copilot model id -> price (GitHub's table on PRICES_AS_OF).
PRICES: Dict[str, ModelPrice] = {
    # OpenAI
    "gpt-5.3-codex": ModelPrice(Rate(1.75, 0.175, 14.00)),
    "gpt-5.4-nano": ModelPrice(Rate(0.20, 0.02, 1.25)),
    "gpt-5.6-luna": _tiers(Rate(0.20, 0.02, 1.20), 200_000, Rate(0.40, 0.04, 1.80)),
    "gpt-5.6-sol": _tiers(Rate(4.00, 0.40, 20.00), 272_000, Rate(8.00, 0.80, 30.00)),
    "gpt-5.6-terra": _tiers(Rate(2.00, 0.20, 12.00), 272_000, Rate(4.00, 0.40, 18.00)),
    "gpt-6-astra": _tiers(Rate(10.00, 1.00, 50.00), 272_000, Rate(20.00, 2.00, 75.00)),
    "gpt-6-luna": _tiers(Rate(0.10, 0.01, 0.50), 272_000, Rate(0.20, 0.02, 0.75)),
    "gpt-6-sol": _tiers(Rate(2.00, 0.20, 10.00), 272_000, Rate(4.00, 0.40, 15.00)),
    "gpt-6.1-sol": _tiers(Rate(2.00, 0.10, 10.00), 272_000, Rate(4.00, 0.20, 15.00)),
    "gpt-6.1-terra": _tiers(Rate(1.00, 0.05, 5.00), 272_000, Rate(2.00, 0.10, 7.50)),
    # Anthropic
    "claude-haiku-4.5": ModelPrice(Rate(1.00, 0.10, 5.00)),
    "claude-haiku-5.5": _tiers(Rate(0.10, 0.01, 0.50), 100_000, Rate(0.50, 0.05, 2.50)),
    "claude-sonnet-4": ModelPrice(Rate(3.00, 0.30, 15.00)),
    "claude-sonnet-4.6": ModelPrice(Rate(3.00, 0.30, 15.00)),
    "claude-sonnet-5": ModelPrice(Rate(2.00, 0.20, 10.00)),
    "claude-sonnet-5.5": ModelPrice(Rate(2.00, 0.10, 10.00)),
    "claude-opus-4.8": ModelPrice(Rate(5.00, 0.50, 25.00)),
    "claude-opus-5": ModelPrice(Rate(5.00, 0.50, 25.00)),
    "claude-opus-5.5": ModelPrice(Rate(4.00, 0.20, 20.00)),
    "claude-fable-5": ModelPrice(Rate(10.00, 1.00, 50.00)),
    "claude-fable-5.1": ModelPrice(Rate(10.00, 0.25, 50.00)),
    "claude-fable-5.2": ModelPrice(Rate(8.00, 0.20, 40.00)),
    # Google (promotional prices; GitHub has not published the later price of Flash and Argon)
    "gemini-3.8-flash": ModelPrice(Rate(0.75, 0.075, 3.75)),
    "gemini-4-argon": ModelPrice(Rate(2.00, 0.10, 10.00)),
    "gemini-4-carbon": ModelPrice(Rate(2.00, 0.10, 10.00), until="2026-12-01", after=Rate(4.00, 0.20, 20.00)),
    # xAI
    "grok-4.5": _GROK,
    "grok-4.6": _GROK,
    "grok-4.7": _GROK,
    # Microsoft and Moonshot AI
    "mai-code-1.1-flash": ModelPrice(Rate(0.20, 0.02, 1.20)),
    "kimi-k3": ModelPrice(Rate(3.00, 0.30, 15.00)),
}


def _key(model: str) -> str:
    """'anthropic/Claude-Sonnet-4-6' and 'claude-sonnet-4.6' compare equal."""
    name = str(model or "").split("/", 1)[-1].strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


_INDEX = {_key(model): price for model, price in PRICES.items()}


def lookup(model: str) -> Optional[ModelPrice]:
    return _INDEX.get(_key(model))


def estimate(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0,
             today: Optional[date] = None) -> Optional[float]:
    """USD for one call. `prompt_tokens` includes the cached ones. None when GitHub lists no rate."""
    price = lookup(model)
    if price is None:
        return None
    rate = price.rate(prompt_tokens, today)
    cached = max(0, min(cached_tokens, prompt_tokens))
    fresh = prompt_tokens - cached
    usd = (fresh * rate.input + cached * rate.cached + completion_tokens * rate.output) / 1_000_000
    return round(usd, 8)


def note(missing_calls: int = 0) -> str:
    text = (f"Estimación con las tarifas por token de GitHub Copilot a {PRICES_AS_OF} (1 crédito de IA = 0,01 $): "
            "es lo que la limpieza descuenta de los créditos de tu plan. No aplica a las suscripciones anuales "
            "Pro/Pro+, que siguen pagando por peticiones premium.")
    if missing_calls:
        text += f" {missing_calls} llamadas a modelos sin tarifa publicada no están incluidas."
    return text
