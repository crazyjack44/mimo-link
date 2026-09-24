"""mimo-link core: keep the MiMo Desktop capability API wired into Hermes.

Stdlib-only so it runs both inside the Hermes plugin and standalone:
    python mimo_link_core.py [sync|status|models|route|export|token] [--model MODEL] [--dir DIR]

What it does (the "link"):
  1. finds the Xiaomi MiMo Desktop install dir / running PIDs,
  2. finds the live capability port (it drifts every desktop session),
  3. ensures a scoped token in the ENGINE's token registry
     (XDG_STATE_HOME gotcha: the `mimo` CLI appends \\mimocode itself),
  4. writes MIMO_LLM_SERVER_TOKEN into <hermes-home>/.env,
  5. `route` applies the link to a target (`--target hermes|codex`):
     - hermes: `hermes config set` for `mimo-desktop` alias
     - codex:  merge-write ~/.codex/config.toml + auth.json
     `export` emits CC Switch JSON (settingsConfig per target format).

The plaintext token is never printed by default — only a 6-char mask.
`action=token` / export config intentionally return the full token for
local clipboard / CC Switch import only.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

ALIAS_NAME = "mimo-desktop"
DEFAULT_MODEL = "mimo-desktop/mimo-v2.6-pro"
ENV_KEY = "MIMO_LLM_SERVER_TOKEN"
# Managed (mlk_) keys never overwrite the engine scoped token; they live here.
MANAGED_ENV_KEY = "MIMO_LINK_API_KEY"
MANAGED_KEY_PREFIX = "mlk_"
# Never advertise these model ids to clients / model discovery.
HIDDEN_MODEL_PREFIXES = ("xiaomi/",)
TOKEN_LABEL = "hermes"
PROC_NAME = "Xiaomi MiMo.exe"
PROBE_TIMEOUT = 1.5
# All mimo-link state (keys / pricing / engine token) lives in this plugin folder.
PLUGIN_ROOT = Path(__file__).resolve().parent
ENV_FILE_NAME = "mimo-link.env"


def data_dir() -> Path:
    """Local data dir for mimo-link — never HERMES_HOME.

    Frozen (PyInstaller) builds keep state in %LOCALAPPDATA%/mimo-link so keys,
    pricing and the engine token survive overwrites of the app folder.
    """
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "mimo-link"
    return PLUGIN_ROOT


def env_file_path() -> Path:
    return data_dir() / ENV_FILE_NAME


# --------------------------------------------------------------------------- paths

def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # inside the Hermes process
        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env)
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes"
        return Path.home() / ".hermes"


def mimo_state_root() -> Path:
    """Where the desktop engine keeps llm-server/<dir-hash>/. XDG_STATE_HOME is
    <appdata>/Xiaomi MiMo and the engine appends \\mimocode itself."""
    if os.name != "nt":
        raise RuntimeError("mimo-link currently supports Windows only")
    return Path(os.environ["APPDATA"]) / "Xiaomi MiMo" / "mimocode"


def mimo_xdg_state_home() -> str:
    return str(mimo_state_root().parent)


# --------------------------------------------------------------------------- discovery

def _proc_pids(name: str) -> list[int]:
    out = subprocess.run(
        ["tasklist", "/fi", f"imagename eq {name}", "/fo", "csv", "/nh"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    ).stdout
    pids = []
    for line in out.splitlines():
        m = re.match(r'"[^"]*",\s*"(\d+)"', line.strip())
        if m:
            pids.append(int(m.group(1)))
    return pids


def _exe_path(pid: int) -> str | None:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def discover_install_dir() -> Path | None:
    """The directory the desktop engine is bound to (its token-registry hash key)."""
    for pid in _proc_pids(PROC_NAME):
        p = _exe_path(pid)
        if p:
            return Path(p).parent
    return None


def mimo_pids() -> list[int]:
    return _proc_pids(PROC_NAME)


def dir_hash(directory: Path) -> str:
    resolved = str(Path(directory).resolve())
    return hashlib.sha1(resolved.encode()).hexdigest()


# --------------------------------------------------------------------------- http

def _get(url: str, token: str | None) -> tuple[int, str]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200).decode("utf-8", "replace")
    except Exception:
        return 0, ""


def _listening_ports(pids: list[int]) -> list[int]:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30).stdout
    ports = set()
    for line in out.splitlines():
        line = line.strip().replace("\r", "")
        parts = line.split()
        if len(parts) < 5 or not parts[0].startswith("TCP") or parts[3] != "LISTENING":
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid in pids:
            try:
                ports.add(int(parts[1].rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                continue
    return sorted(ports, reverse=True)


def find_endpoint(token: str, pids: list[int]) -> tuple[int | None, list[dict] | None]:
    """(port, models) for the first port serving /v1/models with this token."""
    for port in _listening_ports(pids):
        status, body = _get(f"http://127.0.0.1:{port}/v1/models", token)
        if status == 200:
            try:
                return port, filter_public_models(json.loads(body).get("data", []))
            except Exception:
                return port, None
    return None, None


# --------------------------------------------------------------------------- token

def _read_env_var(key: str) -> str:
    envp = env_file_path()
    if not envp.exists():
        return ""
    for line in envp.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def _write_env_var(key: str, token: str) -> None:
    envp = env_file_path()
    lines = []
    if envp.exists():
        lines = [l for l in envp.read_text(encoding="utf-8", errors="replace").splitlines()
                 if not l.startswith(f"{key}=")]
    lines.append(f"{key}={token}")
    envp.parent.mkdir(parents=True, exist_ok=True)
    envp.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_env_token(home: Path | None = None) -> str:
    """Read engine scoped token (MIMO_LLM_SERVER_TOKEN) from plugin-folder mimo-link.env."""
    return _read_env_var(ENV_KEY)


def write_env_token(token: str, home: Path | None = None) -> None:
    """Persist the engine scoped token only. Never used for managed mlk_ keys."""
    _write_env_var(ENV_KEY, token)


def read_managed_key() -> str:
    return _read_env_var(MANAGED_ENV_KEY)


def write_managed_key(token: str) -> None:
    """Persist a managed mlk_ key separately from the engine scoped token."""
    _write_env_var(MANAGED_ENV_KEY, token)


def is_managed_key(token: str | None) -> bool:
    return bool((token or "").strip().startswith(MANAGED_KEY_PREFIX))


def is_hidden_model(model_id: str | None) -> bool:
    mid = (model_id or "").strip().lower()
    return any(mid.startswith(p) for p in HIDDEN_MODEL_PREFIXES)


def filter_public_models(models: list | None) -> list:
    """Drop hidden provider models (e.g. xiaomi/*) from any client-facing list."""
    if not models:
        return []
    out = []
    for m in models:
        mid = m.get("id") if isinstance(m, dict) else m
        if is_hidden_model(mid if isinstance(mid, str) else ""):
            continue
        out.append(m)
    return out


def mint_token(install_dir: Path) -> dict:
    """`mimo llm-server issue` into the ENGINE's registry (the XDG_STATE_HOME dance)."""
    mimo = shutil.which("mimo")
    if not mimo:
        raise RuntimeError("`mimo` CLI not found on PATH (expected ~/.mimocode/bin)")
    env = dict(os.environ, XDG_STATE_HOME=mimo_xdg_state_home())
    out = subprocess.run(
        [mimo, "llm-server", "issue", "--json", "--label", TOKEN_LABEL,
         "--ttl", "none", "--max-age", "none"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(install_dir), timeout=60,
    )
    try:
        data = json.loads(out.stdout)
    except Exception:
        raise RuntimeError(f"mimo llm-server issue failed: {out.stdout[-200:]} {out.stderr[-200:]}")
    if not data.get("api_key"):
        raise RuntimeError(f"issue returned no api_key: {data}")
    # sanity: the token must have landed in the engine's registry for this dir
    reg = mimo_state_root() / "llm-server" / dir_hash(install_dir) / "tokens.json"
    if not reg.exists() or data.get("id", "") not in reg.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError(f"token did not land in the engine registry ({reg}) — wrong --dir?")
    return data


def mint_llm_server_token(install_dir: str | None = None) -> dict:
    """Create/refresh the unified engine scoped token and persist it to HERMES .env.

    This is the root credential for mimo-link: managed `mlk_` keys only meter and
    forward after this engine token exists. Must be regenerated on each machine.
    """
    home = hermes_home()
    directory = Path(install_dir) if install_dir else discover_install_dir()
    if not directory:
        raise RuntimeError(
            "MiMo Desktop install dir not found — start Desktop (or pass install_dir) first")
    data = mint_token(directory)
    token = data.get("api_key") or ""
    if not token:
        raise RuntimeError("issue returned empty api_key")
    write_env_token(token, home)
    return {
        "token": token,
        "mask": mask(token),
        "token_state": "minted",
        "install_dir": str(directory),
        "data_dir": str(data_dir()),
        "env_path": str(env_file_path()),
        "label": TOKEN_LABEL,
        "id": data.get("id"),
        "note": "统一 scoped token（引擎侧 MIMO_LLM_SERVER_TOKEN）——必须先生成，后续 mlk_ Key 才能正常分发/转发",
    }


def mask(token: str) -> str:
    return (token[:6] + "***") if token else "(none)"


# --------------------------------------------------------------------------- hermes config

def _hermes_bin() -> str | None:
    """Hermes CLI is optional — Codex / sync / keys must work without it."""
    return shutil.which("hermes")


def read_alias() -> dict:
    """Read Hermes model alias. Returns {} when hermes CLI is missing."""
    exe = _hermes_bin()
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "config", "get", f"model_aliases.{ALIAS_NAME}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except Exception:
        return {}
    entry = {}
    for line in out.stdout.splitlines():
        m = re.match(r"\s*(model|base_url|provider|key_env):\s*(\S+)", line)
        if m:
            entry[m.group(1)] = m.group(2)
    return entry


def write_alias(model: str, base_url: str, key_env: str = ENV_KEY) -> None:
    exe = _hermes_bin()
    if not exe:
        raise RuntimeError(
            "`hermes` not found on PATH — 仅「路由至 Hermes」需要它；"
            "Codex / 同步 / Key 不依赖 Hermes")
    payload = json.dumps({"model": model, "provider": "custom",
                          "base_url": base_url, "key_env": key_env})
    subprocess.run([exe, "config", "set", f"model_aliases.{ALIAS_NAME}", payload],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def hermes_available() -> bool:
    return _hermes_bin() is not None


# --------------------------------------------------------------------------- codex config

CODEX_PROVIDER_KEY = ALIAS_NAME  # "mimo-desktop"
# Codex speaks Responses API; mimo-link bridges /v1/responses → MiMo chat.completions
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765/v1"


def bridge_url() -> str:
    return os.environ.get("MIMO_LINK_BRIDGE_URL") or DEFAULT_BRIDGE_URL


def codex_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex"
    return Path.home() / ".codex"


def _backup_file(path: Path) -> str:
    if not path.exists():
        return ""
    bak = path.with_name(path.name + ".mimo-link.bak")
    shutil.copy2(path, bak)
    return str(bak)


def build_codex_config_toml(base_url: str, model: str) -> str:
    """Codex ~/.codex/config.toml fragment.

    Codex removed wire_api=chat (see openai/codex#7782) — must be "responses".
    base_url should point at the mimo-link bridge which speaks /v1/responses
    and forwards to MiMo /v1/chat/completions.
    """
    return (
        f'model_provider = "{CODEX_PROVIDER_KEY}"\n'
        f"model = {json.dumps(model)}\n"
        f"disable_response_storage = true\n"
        f"\n"
        f"[model_providers.{CODEX_PROVIDER_KEY}]\n"
        f'name = "MiMo Desktop"\n'
        f"base_url = {json.dumps(base_url)}\n"
        f'wire_api = "responses"\n'
        f"requires_openai_auth = true\n"
    )


def build_codex_auth_json(token: str) -> str:
    """CC Switch Codex auth.json body (import into CC Switch)."""
    return json.dumps({"OPENAI_API_KEY": token}, ensure_ascii=False, indent=2) + "\n"


def build_cc_switch_codex_files(
    token: str,
    model: str,
    base_url: str | None = None,
) -> dict:
    """Standard CC Switch Codex import pair (NOT the live ~/.codex files)."""
    base = base_url or bridge_url()
    return {
        "format": "cc-switch-codex",
        "base_url": base,
        "model": model,
        "wire_api": "responses",
        "config_toml": build_codex_config_toml(base, model),
        "auth_json": build_codex_auth_json(token),
        "note": "CC Switch 导入用标准 config.toml / auth.json（非本机 ~/.codex 实时文件）",
    }


def _parse_simple_toml(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Split TOML into top-level lines and {section_name: body_lines} (headers dropped)."""
    top: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and not s.startswith("[["):
            current = s[1:-1].strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            top.append(line)
        else:
            sections[current].append(line)
    return top, sections


def _toml_key(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("[") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip().strip('"').strip("'")


def _set_top_keys(top: list[str], updates: dict[str, str]) -> list[str]:
    """Replace or append top-level key = value lines, keep comments/blank."""
    managed = set(updates)
    out: list[str] = []
    seen: set[str] = set()
    for line in top:
        k = _toml_key(line)
        if k in managed:
            if k not in seen:
                out.append(f"{k} = {updates[k]}")
                seen.add(k)
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k} = {v}")
            seen.add(k)
    # trim leading/trailing excess blanks
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def _section_body(updates: dict[str, str]) -> list[str]:
    return [f"{k} = {v}" for k, v in updates.items()]


def write_codex_config(base_url: str, model: str, token: str) -> dict:
    """Merge-write ~/.codex/config.toml + auth.json. Backs up both first.

    Only touches mimo-link managed keys; leaves other Codex settings intact.
    """
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    info: dict = {"home": str(home), "config_path": str(config_path), "auth_path": str(auth_path)}

    # ---- auth.json (merge OPENAI_API_KEY only)
    info["auth_backup"] = _backup_file(auth_path)
    auth: dict = {}
    if auth_path.exists():
        try:
            loaded = json.loads(auth_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                auth = loaded
        except Exception:
            auth = {}
    auth["OPENAI_API_KEY"] = token
    auth_path.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---- config.toml (merge managed top keys + [model_providers.<key>])
    info["config_backup"] = _backup_file(config_path)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    top, sections = _parse_simple_toml(text)

    top_updates = {
        "model_provider": json.dumps(CODEX_PROVIDER_KEY),
        "model": json.dumps(model),
        "disable_response_storage": "true",
    }
    # keep an existing model_reasoning_effort if present; do not force it
    new_top = _set_top_keys(top, top_updates)

    prov_section = f"model_providers.{CODEX_PROVIDER_KEY}"
    sections[prov_section] = _section_body({
        "name": json.dumps("MiMo Desktop"),
        "base_url": json.dumps(base_url),
        "wire_api": json.dumps("responses"),
        "requires_openai_auth": "true",
    })

    lines_out: list[str] = list(new_top)
    for name, body in sections.items():
        if not any(x.strip() for x in body) and name != prov_section:
            continue
        if lines_out and lines_out[-1].strip():
            lines_out.append("")
        lines_out.append(f"[{name}]")
        lines_out.extend(body)

    config_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")

    # Codex hard-errors on ANY wire_api = "chat" in the file (openai/codex#7782)
    text2 = config_path.read_text(encoding="utf-8")
    if 'wire_api = "chat"' in text2:
        config_path.write_text(text2.replace('wire_api = "chat"', 'wire_api = "responses"'), encoding="utf-8")

    info["updated"] = {
        "model_provider": CODEX_PROVIDER_KEY,
        "model": model,
        "base_url": base_url,
        "wire_api": "responses",
    }
    return info


def read_codex_summary() -> dict:
    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    summary = {
        "home": str(home),
        "config_exists": config_path.exists(),
        "auth_exists": auth_path.exists(),
        "model_provider": None,
        "model": None,
        "base_url": None,
        "wire_api": None,
        "has_api_key": False,
    }
    if config_path.exists():
        top, sections = _parse_simple_toml(config_path.read_text(encoding="utf-8"))
        for line in top:
            k = _toml_key(line)
            if k == "model_provider":
                summary["model_provider"] = line.split("=", 1)[1].strip().strip('"')
            elif k == "model":
                summary["model"] = line.split("=", 1)[1].strip().strip('"')
        prov = sections.get(f"model_providers.{CODEX_PROVIDER_KEY}", [])
        for line in prov:
            k = _toml_key(line)
            if k == "base_url":
                summary["base_url"] = line.split("=", 1)[1].strip().strip('"')
            elif k == "wire_api":
                summary["wire_api"] = line.split("=", 1)[1].strip().strip('"')
    if auth_path.exists():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            summary["has_api_key"] = bool(isinstance(auth, dict) and auth.get("OPENAI_API_KEY"))
        except Exception:
            pass
    return summary


def read_codex_files() -> dict:
    """Full text of ~/.codex/config.toml and auth.json for UI display / copy."""
    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    return {
        "home": str(home),
        "config_path": str(config_path),
        "auth_path": str(auth_path),
        "config_toml": config_path.read_text(encoding="utf-8", errors="replace") if config_path.exists() else "",
        "auth_json": auth_path.read_text(encoding="utf-8", errors="replace") if auth_path.exists() else "",
        "config_exists": config_path.exists(),
        "auth_exists": auth_path.exists(),
    }


# --------------------------------------------------------------------------- link state cache
# Last prepared endpoint (from sync). Routing/export read this so a second
# button click can apply without re-probing. In-process only.

_PREPARED: dict = {}


def get_plain_token(home: Path | None = None) -> str:
    """Full token for local copy/export. Never print this in logs."""
    return read_env_token(home)


def build_cc_switch_config(
    token: str,
    base_url: str,
    model: str,
    models: list | None = None,
    provider_id: str = "mimo-desktop",
    target: str = "hermes",
) -> dict:
    """CC Switch AppConfig shape: { providers: Record<string, Provider>, current }.

    target=hermes → settingsConfig is Hermes custom_provider fields.
    target=codex  → settingsConfig is { auth, config } (config = TOML string).
    """
    model_entries = []
    for m in models or []:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("model") or ""
            if not mid:
                continue
            entry: dict = {"id": mid}
            if m.get("name"):
                entry["name"] = m["name"]
            if m.get("context_length"):
                entry["context_length"] = m["context_length"]
            model_entries.append(entry)
        elif isinstance(m, str) and m:
            model_entries.append({"id": m})

    if model and not any(e.get("id") == model for e in model_entries):
        model_entries.insert(0, {"id": model})

    target = (target or "hermes").lower()
    if target == "codex":
        settings = {
            "auth": {"OPENAI_API_KEY": token},
            "config": build_codex_config_toml(base_url, model),
        }
        display_name = "MiMo Desktop (Codex)"
        notes = "Xiaomi MiMo Desktop capability API for Codex (via mimo-link)"
    else:
        settings = {
            "name": ALIAS_NAME,
            "base_url": base_url,
            "api_key": token,
            "api_mode": "chat_completions",
            "models": model_entries,
        }
        display_name = "MiMo Desktop"
        notes = "Xiaomi MiMo Desktop local capability API (via mimo-link)"

    provider = {
        "id": provider_id,
        "name": display_name,
        "settingsConfig": settings,
        "websiteUrl": "https://mimo.xiaomi.com",
        "category": "custom",
        "createdAt": int(__import__("time").time() * 1000),
        "notes": notes,
        "icon": "openai",
        "iconColor": "#0071E3",
    }
    return {
        "providers": {provider_id: provider},
        "current": provider_id,
    }


def _ensure_endpoint(
    directory: Path | None,
    install_dir: str | None,
    pids: list[int],
    home: Path,
) -> tuple[dict, str, int, list | None]:
    """Validate desktop + mint/keep token + find live port.

    Returns (error_result_or_empty, token, port, models).
    """
    if not pids:
        return ({"error": "MiMo Desktop is not running — start it (with a session open) first"}, "", 0, None)
    if not directory:
        return ({"error": "could not locate the MiMo install dir — pass --dir"}, "", 0, None)

    token = read_env_token(home)
    token_state = "kept" if token else "minted"
    if not token:
        if not install_dir:
            return ({"error": (
                "no token in .env and MiMo install dir not auto-detected — "
                "pass --dir <MiMo install dir> so the token can be minted")}, "", 0, None)
        token = mint_token(directory)["api_key"]
        write_env_token(token, home)

    port, models = find_endpoint(token, pids)
    if not port:  # stale/revoked token: mint a fresh one and retry
        if not install_dir:
            return ({"error": (
                "existing token is rejected and install dir not auto-detected — "
                "pass --dir <MiMo install dir> to mint a fresh token")}, "", 0, None)
        token = mint_token(directory)["api_key"]
        write_env_token(token, home)
        token_state = "reminted"
        port, models = find_endpoint(token, pids)
    if not port:
        return ({"error": "capability endpoint not found on any MiMo Desktop port (engine up?)"}, "", 0, None)

    _PREPARED.update({
        "token": token,
        "token_state": token_state,
        "live_port": port,
        "base_url": f"http://127.0.0.1:{port}/v1",
        "models": models,
    })
    return ({}, token, port, models)


# --------------------------------------------------------------------------- actions

def run_action(
    action: str = "status",
    model: str | None = None,
    install_dir: str | None = None,
    target: str = "hermes",
    export_format: str | None = None,
    api_key: str | None = None,
) -> dict:
    home = hermes_home()
    result: dict = {
        "action": action,
        "alias": ALIAS_NAME,
        "data_dir": str(data_dir()),
        "hermes_available": hermes_available(),
    }

    directory = Path(install_dir) if install_dir else discover_install_dir()
    pids = mimo_pids()
    result["desktop_running"] = bool(pids)
    result["install_dir"] = str(directory) if directory else None
    route_target = (target or "hermes").lower()
    result["route_target"] = route_target

    if action == "status":
        token = read_env_token()
        result["env_token"] = mask(token)
        result["hermes_available"] = hermes_available()
        result["alias_config"] = read_alias()  # {} if hermes CLI missing
        result["codex_config"] = read_codex_summary()
        port, models = (None, None)
        if token and pids:
            port, models = find_endpoint(token, pids)
        result["live_port"] = port
        result["models"] = [m.get("id") for m in models] if models else None
        result["linked"] = bool(port) and read_alias().get("base_url", "").endswith(f":{port}/v1")
        return result

    if action == "token":
        token = read_env_token(home)
        if not token:
            result["error"] = "no token in .env — run action=sync first"
            return result
        result["token"] = token  # plaintext for local clipboard only
        return result

    if action == "codex_files":
        result.update(read_codex_files())
        return result

    if action == "models":
        token = read_env_token(home)
        if not token or not pids:
            result["error"] = "no token or MiMo Desktop not running — run action=sync"
            return result
        port, models = find_endpoint(token, pids)
        if not port:
            result["error"] = "capability endpoint not found (desktop engine up?)"
            return result
        result["live_port"] = port
        result["models"] = [m.get("id") for m in models] if models else []
        return result

    if action not in ("sync", "route", "export"):
        result["error"] = f"unknown action {action!r}"
        return result

    # ---- sync: prepare endpoint/token only (does NOT write Hermes alias)
    if action == "sync":
        err, token, port, models = _ensure_endpoint(directory, install_dir, pids, home)
        if err:
            result.update(err)
            return result
        alias = read_alias()
        new_model = model or alias.get("model") or DEFAULT_MODEL
        upstream_base = f"http://127.0.0.1:{port}/v1"
        new_base = bridge_url()
        _PREPARED["model"] = new_model
        _PREPARED["upstream_base"] = upstream_base
        result.update({
            "token": mask(token),
            "token_state": _PREPARED.get("token_state"),
            "live_port": port,
            "upstream_base": upstream_base,
            "bridge_url": bridge_url(),
            "base_url": new_base,
            "model": new_model,
            "alias_updated": False,
            "routed": False,
            "ready_to_route": True,
            "models": [m.get("id") for m in models] if models else [],
        })
        return result

    # ---- route: write target config (explicit user action)
    if action == "route":
        prepared_port = _PREPARED.get("live_port")
        prepared_model = _PREPARED.get("model")
        prepared_token = _PREPARED.get("token") or read_env_token(home)
        prepared_models = _PREPARED.get("models")

        if prepared_port and prepared_token:
            port, token, models = int(prepared_port), prepared_token, prepared_models
        else:
            err, token, port, models = _ensure_endpoint(directory, install_dir, pids, home)
            if err:
                result.update(err)
                return result

        alias = read_alias()
        new_model = model or prepared_model or alias.get("model") or DEFAULT_MODEL
        # Codex must use the local Responses bridge (wire_api=responses)
        new_base = bridge_url() if route_target == "codex" else f"http://127.0.0.1:{port}/v1"
        upstream_base = f"http://127.0.0.1:{port}/v1"

        if route_target == "codex":
            new_base = bridge_url()  # never write the raw MiMo port into Codex
            # Prefer user-supplied managed key (mlk_) so traffic is metered
            supplied = (api_key or "").strip()
            write_token = supplied or token
            if is_managed_key(supplied):
                write_managed_key(supplied)
            try:
                codex_info = write_codex_config(new_base, new_model, write_token)
            except Exception as e:
                result["error"] = f"codex route failed: {e}"
                return result
            result.update({
                "token": mask(write_token),
                "live_port": port,
                "upstream_base": upstream_base,
                "base_url": new_base,
                "model": new_model,
                "alias_updated": False,
                "routed": True,
                "route_target": "codex",
                "wire_api": "responses",
                "config_key": write_token if supplied else None,
                "engine_token": mask(token),
                "codex": codex_info,
                "models": [m.get("id") for m in models] if models else [],
            })
            return result

        if route_target != "hermes":
            result["error"] = f"unknown target {route_target!r} (use hermes|codex)"
            return result

        supplied = (api_key or "").strip()
        if not hermes_available():
            result["error"] = (
                "`hermes` not found on PATH — 无法路由到 Hermes。"
                "可改选「Codex」，或安装 hermes CLI。同步 / Key / Codex 均不依赖 Hermes。")
            return result
        # Engine scoped token is the root credential and must never be replaced
        # by a managed mlk_ key. Managed keys go through the :8765 bridge.
        if is_managed_key(supplied):
            write_managed_key(supplied)
            write_token = supplied
            new_base = bridge_url()
            key_env = MANAGED_ENV_KEY
        elif supplied:
            write_managed_key(supplied)
            write_token = supplied
            new_base = f"http://127.0.0.1:{port}/v1"
            key_env = MANAGED_ENV_KEY
        else:
            write_token = token
            new_base = f"http://127.0.0.1:{port}/v1"
            key_env = ENV_KEY
        write_alias(new_model, new_base, key_env=key_env)
        result.update({
            "token": mask(write_token),
            "live_port": port,
            "upstream_base": upstream_base,
            "base_url": new_base,
            "model": new_model,
            "alias_updated": True,
            "routed": True,
            "route_target": "hermes",
            "key_env": key_env,
            "config_key": write_token if supplied else None,
            "engine_token": mask(token),
            "models": [m.get("id") for m in models] if models else [],
        })
        return result

    # ---- export: CC Switch AppConfig (hermes or codex settingsConfig)
    if action == "export":
        prepared_port = _PREPARED.get("live_port")
        prepared_token = _PREPARED.get("token") or read_env_token(home)
        prepared_models = _PREPARED.get("models")
        prepared_model = _PREPARED.get("model")

        if prepared_port and prepared_token:
            port, token, models = int(prepared_port), prepared_token, prepared_models
        else:
            err, token, port, models = _ensure_endpoint(directory, install_dir, pids, home)
            if err:
                result.update(err)
                return result

        alias = read_alias()
        new_model = model or prepared_model or alias.get("model") or DEFAULT_MODEL
        fmt = (export_format or route_target or "hermes").lower()
        if fmt not in ("hermes", "codex"):
            result["error"] = f"unknown export format {fmt!r} (use hermes|codex)"
            return result
        supplied = (api_key or "").strip()
        write_token = supplied or token
        if fmt == "codex" or is_managed_key(supplied):
            new_base = bridge_url()
        else:
            new_base = f"http://127.0.0.1:{port}/v1"
        cfg = build_cc_switch_config(write_token, new_base, new_model, models, target=fmt)
        result.update({
            "token": mask(write_token),
            "live_port": port,
            "upstream_base": f"http://127.0.0.1:{port}/v1",
            "base_url": new_base,
            "model": new_model,
            "config_format": "cc-switch",
            "export_format": fmt,
            "config_key": write_token if supplied else None,
            "engine_token": mask(token),
            "config": cfg,
            "models": [m.get("id") for m in models] if models else [],
        })
        return result


# --------------------------------------------------------------------------- standalone CLI

def main() -> int:
    ap = argparse.ArgumentParser(prog="mimo_link_core", description=__doc__)
    ap.add_argument("action", nargs="?", default="status",
                    choices=["sync", "status", "models", "route", "export", "token", "codex_files"])
    ap.add_argument("--model", help=f"alias model id (default: keep current / {DEFAULT_MODEL})")
    ap.add_argument("--dir", dest="install_dir", help="MiMo install dir (override auto-detect)")
    ap.add_argument("--target", choices=["hermes", "codex"], default="hermes",
                    help="route target (default: hermes)")
    ap.add_argument("--format", dest="export_format", choices=["hermes", "codex"],
                    help="export format (default: same as --target)")
    args = ap.parse_args()
    try:
        report = run_action(
            args.action, model=args.model, install_dir=args.install_dir,
            target=args.target, export_format=args.export_format,
        )
    except Exception as e:  # never raise out of the CLI
        report = {"action": args.action, "error": str(e)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
