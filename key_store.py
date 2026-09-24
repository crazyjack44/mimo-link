"""Managed API keys: generate, quota, and token usage metering.

Keys are local secrets (`mlk_…`) that clients (Hermes / Codex) present to the
mimo-link bridge. The bridge forwards to MiMo with the real engine token and
records prompt/completion token usage per key.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from pathlib import Path

from mimo_link_core import data_dir
from pricing import cost_cny, tokens_to_yuan

STORE_NAME = "mimo-link-keys.json"
KEY_PREFIX = "mlk_"
_OVERRIDE: Path | None = None


def store_path() -> Path:
    if _OVERRIDE is not None:
        return _OVERRIDE
    return data_dir() / STORE_NAME


def _load() -> dict:
    p = store_path()
    if not p.exists():
        return {"keys": [], "updated_at": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"keys": []}
        data.setdefault("keys", [])
        return data
    except Exception:
        return {"keys": [], "updated_at": 0}


def _save(data: dict) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = int(time.time() * 1000)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def mask_token(token: str) -> str:
    if not token:
        return "(none)"
    if len(token) <= 8:
        return token[:2] + "***"
    return token[:8] + "***"


def _public(item: dict) -> dict:
    out = dict(item)
    out["mask"] = mask_token(item.get("token") or "")
    out.pop("token", None)
    i = int(item.get("used_input") or 0)
    o = int(item.get("used_output") or 0)
    out["used_input"] = i
    out["used_output"] = o
    out["used_total"] = int(item.get("used_total") or (i + o))
    out["used_cny"] = cost_cny(i, o)
    q = item.get("quota")
    if q is None:
        out["quota"] = None
        out["quota_cny"] = None
    else:
        try:
            out["quota"] = int(q)
            out["quota_cny"] = tokens_to_yuan(int(q))
        except (TypeError, ValueError):
            out["quota"] = None
            out["quota_cny"] = None
    return out


def list_keys(include_secret: bool = False) -> list[dict]:
    keys = _load().get("keys") or []
    if include_secret:
        return [dict(k) for k in keys]
    return [_public(k) for k in keys]


def get_key(key_id: str) -> dict | None:
    for k in _load().get("keys") or []:
        if k.get("id") == key_id:
            return k
    return None


def get_key_by_secret(token: str) -> dict | None:
    if not token:
        return None
    for k in _load().get("keys") or []:
        if k.get("token") and k.get("token") == token and k.get("enabled", True):
            return k
    return None


def usage_totals() -> dict:
    data = _load()
    tin = tout = ttot = 0
    by_key = []
    for k in data.get("keys") or []:
        i = int(k.get("used_input") or 0)
        o = int(k.get("used_output") or 0)
        t = int(k.get("used_total") or (i + o))
        tin += i
        tout += o
        ttot += t
        cost = cost_cny(i, o)
        quota_cny = None
        q = k.get("quota")
        if q is not None:
            try:
                from pricing import tokens_to_yuan
                quota_cny = tokens_to_yuan(int(q))
            except Exception:
                quota_cny = None
        by_key.append({
            "id": k.get("id"),
            "name": k.get("name"),
            "mask": mask_token(k.get("token") or ""),
            "quota": q,
            "quota_cny": quota_cny,
            "used_input": i,
            "used_output": o,
            "used_total": t,
            "used_cny": cost,
            "enabled": bool(k.get("enabled", True)),
        })
    return {
        "total_tokens": ttot,
        "input_tokens": tin,
        "output_tokens": tout,
        "total_cny": cost_cny(tin, tout),
        "input_cny": cost_cny(tin, 0),
        "output_cny": cost_cny(0, tout),
        "key_count": len(by_key),
        "by_key": by_key,
    }


def create_key(name: str, quota: int | None = None) -> dict:
    name = (name or "").strip() or "untitled"
    token = KEY_PREFIX + secrets.token_hex(24)
    q = None
    try:
        if quota not in (None, "", 0) and int(quota) > 0:
            q = int(quota)
    except (TypeError, ValueError):
        q = None
    item = {
        "id": uuid.uuid4().hex,
        "name": name,
        "token": token,
        "quota": q,
        "used_input": 0,
        "used_output": 0,
        "used_total": 0,
        "enabled": True,
        "created_at": int(time.time() * 1000),
        "last_used_at": None,
        "source": "mimo-link",
    }
    data = _load()
    data.setdefault("keys", []).append(item)
    _save(data)
    return {**_public(item), "token": token}


def update_key(key_id: str, **fields) -> dict | None:
    data = _load()
    for k in data.get("keys") or []:
        if k.get("id") != key_id:
            continue
        if fields.get("name") is not None:
            k["name"] = str(fields["name"]).strip() or k.get("name")
        if "quota" in fields:
            q = fields["quota"]
            if q in (None, "", 0, "0"):
                k["quota"] = None
            else:
                try:
                    k["quota"] = int(q) if int(q) > 0 else None
                except (TypeError, ValueError):
                    pass
        if fields.get("enabled") is not None:
            k["enabled"] = bool(fields["enabled"])
        _save(data)
        return _public(k)
    return None


def delete_key(key_id: str) -> bool:
    data = _load()
    before = data.get("keys") or []
    after = [k for k in before if k.get("id") != key_id]
    if len(after) == len(before):
        return False
    data["keys"] = after
    _save(data)
    return True


def reset_usage(key_id: str | None = None) -> int:
    data = _load()
    n = 0
    for k in data.get("keys") or []:
        if key_id and k.get("id") != key_id:
            continue
        k["used_input"] = 0
        k["used_output"] = 0
        k["used_total"] = 0
        n += 1
    _save(data)
    return n


def check_quota(token: str) -> tuple[bool, str, dict | None]:
    """Validate presented secret. Returns (allowed, reason, key_record)."""
    k = get_key_by_secret(token)
    if not k:
        return False, "unknown or disabled key", None
    quota = k.get("quota")
    used = int(k.get("used_total") or 0)
    if quota is not None and used >= int(quota):
        return False, f"quota exceeded ({used}/{quota} tokens)", k
    return True, "", k


def record_usage(key_id: str, input_tokens: int, output_tokens: int) -> None:
    data = _load()
    for k in data.get("keys") or []:
        if k.get("id") != key_id:
            continue
        i = max(0, int(input_tokens or 0))
        o = max(0, int(output_tokens or 0))
        k["used_input"] = int(k.get("used_input") or 0) + i
        k["used_output"] = int(k.get("used_output") or 0) + o
        k["used_total"] = int(k.get("used_total") or 0) + i + o
        k["last_used_at"] = int(time.time() * 1000)
        _save(data)
        return
