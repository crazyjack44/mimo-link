"""Pricing: map token usage / quota to CNY (元).

Rates are CNY per 1,000,000 tokens. Stored in plugin-folder
mimo-link-pricing.json and editable from the Key UI.
"""

from __future__ import annotations

import json
from pathlib import Path

from mimo_link_core import data_dir

STORE_NAME = "mimo-link-pricing.json"
OVERRIDE: Path | None = None

# Defaults are editable. Units: 元 / 1M tokens.
DEFAULT_PRICING = {
    "currency": "CNY",
    "input_cny_per_1m": 2.0,
    "output_cny_per_1m": 8.0,
    # optional named model overrides: model -> {input_cny_per_1m, output_cny_per_1m}
    "models": {},
    "source": "default (editable) — 请按官网定价页修改",
}


def store_path() -> Path:
    if OVERRIDE is not None:
        return OVERRIDE
    return data_dir() / STORE_NAME


def load_pricing() -> dict:
    p = store_path()
    data = dict(DEFAULT_PRICING)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    data[k] = v
        except Exception:
            pass
    return data


def save_pricing(patch: dict) -> dict:
    data = load_pricing()
    for k in ("input_cny_per_1m", "output_cny_per_1m"):
        if k in patch and patch[k] is not None:
            try:
                data[k] = float(patch[k])
            except (TypeError, ValueError):
                pass
    if "currency" in patch and patch["currency"]:
        data["currency"] = str(patch["currency"])
    if "source" in patch:
        data["source"] = str(patch["source"])
    if "models" in patch and isinstance(patch["models"], dict):
        data["models"] = patch["models"]
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return data


def rates_for(model: str | None = None, pricing: dict | None = None) -> tuple[float, float]:
    """(input_cny_per_1m, output_cny_per_1m)."""
    pr = pricing or load_pricing()
    m = (model or "").strip()
    pin = float(pr.get("input_cny_per_1m") or 0)
    pout = float(pr.get("output_cny_per_1m") or 0)
    if m and isinstance(pr.get("models"), dict):
        hit = pr["models"].get(m)
        if isinstance(hit, dict):
            try:
                pin = float(hit.get("input_cny_per_1m", pin))
                pout = float(hit.get("output_cny_per_1m", pout))
            except (TypeError, ValueError):
                pass
    return pin, pout


def cost_cny(input_tokens: int, output_tokens: int, model: str | None = None, pricing: dict | None = None) -> float:
    pin, pout = rates_for(model, pricing)
    return (max(0, int(input_tokens)) * pin + max(0, int(output_tokens)) * pout) / 1_000_000.0


def blended_cny_per_token(pricing: dict | None = None) -> float:
    pin, pout = rates_for(None, pricing)
    # average in/out price per token, used when converting 元 → token quota
    return (pin + pout) / 2.0 / 1_000_000.0


def yuan_to_tokens(yuan: float, pricing: dict | None = None) -> int:
    """Approximate token quota for a CNY budget (blended in/out rate)."""
    per = blended_cny_per_token(pricing)
    if per <= 0:
        return 0
    return int(round(float(yuan) / per))


def tokens_to_yuan(tokens: int, pricing: dict | None = None) -> float:
    """Approximate CNY for a token quota (blended)."""
    return float(tokens) * blended_cny_per_token(pricing)


def format_cny(value: float) -> str:
    return f"¥{value:.4f}" if value < 1 else f"¥{value:.2f}"
