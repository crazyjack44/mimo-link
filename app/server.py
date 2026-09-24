"""MiMo Link — Apple-style desktop companion app.

Local HTTP UI + JSON API around mimo_link_core (sync / status / models).

Usage:
    python server.py [--port 8765] [--open]
Then open http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

def _resolve_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        for cand in (base / "app", base):
            if (cand / "index.html").exists():
                return cand
        return base / "app"
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()
PLUGIN_DIR = APP_DIR.parent if not getattr(sys, "frozen", False) else APP_DIR
if not getattr(sys, "frozen", False) and str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mimo_link_core import (  # noqa: E402
    DEFAULT_MODEL,
    _PREPARED,
    build_cc_switch_codex_files,
    data_dir,
    discover_install_dir,
    env_file_path,
    find_endpoint,
    get_plain_token,
    is_hidden_model,
    mask,
    mint_llm_server_token,
    mimo_pids,
    read_codex_files,
    read_env_token,
    run_action,
)
from key_store import (  # noqa: E402
    check_quota,
    create_key,
    delete_key,
    get_key,
    get_key_by_secret,
    list_keys,
    record_usage,
    reset_usage,
    store_path,
    update_key,
    usage_series,
    usage_totals,
)
from pricing import (  # noqa: E402
    cost_cny,
    format_cny,
    load_pricing,
    save_pricing,
    tokens_to_yuan,
    yuan_to_tokens,
)
from responses_bridge import (  # noqa: E402
    call_mimo_chat,
    call_mimo_chat_stream,
    chat_chunks_to_response_events,
    chat_to_responses,
    extract_usage,
    responses_to_chat,
)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # Allow the UI to be opened from any local origin (preview / file / other port)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        # CORS preflight for cross-origin UI → 127.0.0.1:8765
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Windowed (PyInstaller) builds may have stderr=None — never crash the request.
        try:
            line = "[http] " + (fmt % args) + "\n"
        except Exception:
            return
        for stream in (sys.stderr, sys.stdout):
            if stream is not None:
                try:
                    stream.write(line)
                    return
                except Exception:
                    continue

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _handle_api(self) -> bool:
        path = urlparse(self.path).path
        # /v1/* is the Codex Responses bridge
        if path.startswith("/v1/"):
            try:
                return self._handle_v1(path)
            except Exception as e:
                self._json(500, {"error": str(e)})
                return True
        if not path.startswith("/api/"):
            return False

        try:
            if path == "/api/status" and self.command == "GET":
                report = run_action("status")
                self._json(200 if "error" not in report else 500, report)
                return True

            if path == "/api/models" and self.command == "GET":
                report = run_action("models")
                self._json(200 if "error" not in report else 500, report)
                return True

            if path == "/api/sync" and self.command == "POST":
                body = self._read_body()
                model = body.get("model") or None
                install_dir = body.get("install_dir") or body.get("dir") or None
                report = run_action("sync", model=model, install_dir=install_dir)
                self._json(200 if "error" not in report else 500, report)
                return True

            if path == "/api/route" and self.command == "POST":
                body = self._read_body()
                model = body.get("model") or None
                install_dir = body.get("install_dir") or body.get("dir") or None
                target = body.get("target") or body.get("route_target") or "hermes"
                api_key = body.get("api_key") or body.get("key") or None
                report = run_action("route", model=model, install_dir=install_dir,
                                    target=str(target), api_key=api_key)
                self._json(200 if "error" not in report else 500, report)
                return True

            if path == "/api/export" and self.command in ("GET", "POST"):
                parsed = urlparse(self.path)
                qs = {}
                if parsed.query:
                    from urllib.parse import parse_qs
                    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                body = self._read_body() if self.command == "POST" else {}
                model = body.get("model") or qs.get("model") or None
                install_dir = body.get("install_dir") or body.get("dir") or qs.get("dir") or None
                target = (body.get("target") or body.get("export_format") or body.get("format")
                          or qs.get("target") or qs.get("format") or "hermes")
                api_key = body.get("api_key") or body.get("key") or qs.get("api_key") or None
                report = run_action("export", model=model, install_dir=install_dir,
                                    target=str(target), export_format=str(target), api_key=api_key)
                if "error" in report:
                    self._json(500, report)
                    return True
                cfg = report.get("config") or {}
                # GET = file download; POST = JSON report (with config)
                if self.command == "POST":
                    self._json(200, report)
                    return True
                filename = "cc-switch-config.json"
                if str(target).lower() == "codex":
                    filename = "cc-switch-codex-config.json"
                payload = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return True

            if path == "/api/token" and self.command == "GET":
                try:
                    token = get_plain_token()
                except Exception as e:
                    self._json(500, {"error": str(e)})
                    return True
                if not token:
                    self._json(404, {"error": "no token — run sync first"})
                    return True
                self._json(200, {"token": token})
                return True

            # Generated CC Switch Codex pair (for import) — not live ~/.codex
            if path in ("/api/codex-cc-switch", "/api/codex_cc_switch") and self.command in ("GET", "POST"):
                qs = {}
                parsed = urlparse(self.path)
                if parsed.query:
                    from urllib.parse import parse_qs
                    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                body = self._read_body() if self.command == "POST" else {}
                model = body.get("model") or qs.get("model") or None
                api_key = body.get("api_key") or body.get("key") or qs.get("api_key") or None
                token = (api_key or "").strip() or read_env_token() or get_plain_token() or ""
                if not model:
                    alias = (run_action("status").get("alias_config") or {})
                    model = alias.get("model") or DEFAULT_MODEL
                try:
                    files = build_cc_switch_codex_files(token, model)
                    files["mask"] = mask(token)
                    self._json(200, files)
                except Exception as e:
                    self._json(500, {"error": str(e)})
                return True

            # Unified engine scoped token (MIMO_LLM_SERVER_TOKEN) — root credential
            if path == "/api/llm-token" and self.command == "GET":
                token = read_env_token()
                ok = bool(token)
                self._json(200, {
                    "exists": ok,
                    "mask": mask(token) if ok else "(none)",
                    "env_path": str(env_file_path()),
                    "data_dir": str(data_dir()),
                    "key": "MIMO_LLM_SERVER_TOKEN",
                    "required": True,
                    "note": "统一 scoped token：必须先生成，后续 mlk_ API Key 才能正常分发与转发",
                    "install_dir": str(discover_install_dir() or ""),
                })
                return True

            if path == "/api/llm-token" and self.command == "POST":
                body = self._read_body() if int(self.headers.get("Content-Length") or 0) > 0 else {}
                install_dir = body.get("install_dir") or body.get("dir") or None
                try:
                    report = mint_llm_server_token(install_dir)
                    self._json(200, report)
                except Exception as e:
                    self._json(500, {"error": str(e)})
                return True

            if path in ("/api/codex-files", "/api/codex_files") and self.command == "GET":
                try:
                    self._json(200, read_codex_files())
                except Exception as e:
                    self._json(500, {"error": str(e)})
                return True

            # ---- managed API keys + usage
            if path == "/api/keys" and self.command == "GET":
                self._json(200, {"keys": list_keys(), "usage": usage_totals(),
                                 "store": str(store_path()), "pricing": load_pricing()})
                return True

            if path == "/api/keys" and self.command == "POST":
                body = self._read_body()
                if body.get("action") == "reset_usage":
                    n = reset_usage(body.get("id"))
                    self._json(200, {"reset": n, "usage": usage_totals(), "keys": list_keys()})
                    return True
                quota = body.get("quota")
                # support setting quota in 元
                if body.get("quota_cny") not in (None, ""):
                    try:
                        quota = yuan_to_tokens(float(body.get("quota_cny")))
                    except (TypeError, ValueError):
                        pass
                created = create_key(body.get("name"), quota)
                self._json(201, {"key": created, "keys": list_keys(), "usage": usage_totals()})
                return True

            # /api/keys/<id>/token must be matched BEFORE /api/keys/<id>
            if path.startswith("/api/keys/") and path.endswith("/token") and self.command == "GET":
                parts = path.strip("/").split("/")
                key_id = parts[2] if len(parts) >= 3 else ""
                k = get_key(key_id)
                if not k:
                    self._json(404, {"error": "key not found"})
                    return True
                self._json(200, {"token": k.get("token"), "id": k.get("id")})
                return True

            if path.startswith("/api/keys/") and self.command in ("PATCH", "PUT", "DELETE", "GET"):
                key_id = path.strip("/").split("/")[-1]
                if self.command == "DELETE":
                    ok = delete_key(key_id)
                    self._json(200 if ok else 404, {"deleted": ok, "keys": list_keys(), "usage": usage_totals()})
                    return True
                if self.command in ("PATCH", "PUT"):
                    body = self._read_body()
                    quota_val = body.get("quota")
                    if body.get("quota_cny") not in (None, ""):
                        try:
                            quota_val = yuan_to_tokens(float(body.get("quota_cny")))
                        except (TypeError, ValueError):
                            pass
                    updated = update_key(
                        key_id,
                        name=body.get("name"),
                        quota=quota_val,
                        enabled=body.get("enabled"),
                    )
                    if not updated:
                        self._json(404, {"error": "key not found"})
                        return True
                    self._json(200, {"key": updated, "keys": list_keys(), "usage": usage_totals()})
                    return True
                for k in list_keys():
                    if k.get("id") == key_id:
                        self._json(200, {"key": k})
                        return True
                self._json(404, {"error": "key not found"})
                return True

            if path == "/api/usage" and self.command == "GET":
                self._json(200, usage_totals())
                return True

            if path == "/api/usage/series" and self.command == "GET":
                from urllib.parse import parse_qs
                qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                range_key = (qs.get("range") or qs.get("range_key") or "30d").strip().lower()
                self._json(200, usage_series(range_key))
                return True

            if path == "/api/pricing" and self.command == "GET":
                pr = load_pricing()
                self._json(200, {
                    "pricing": pr,
                    "blended_cny_per_1m": (float(pr.get("input_cny_per_1m") or 0)
                                           + float(pr.get("output_cny_per_1m") or 0)) / 2,
                    "formula": {
                        "cost": "cost_元 = input_tokens/1e6 × 输入价 + output_tokens/1e6 × 输出价",
                        "quota_from_yuan": "token额度 ≈ 预算元 / ((输入价+输出价)/2/1e6)",
                    },
                })
                return True

            if path == "/api/pricing" and self.command in ("POST", "PUT", "PATCH"):
                body = self._read_body()
                # allow yuan → token quota conversion helper
                if body.get("convert_yuan_to_tokens") is not None:
                    self._json(200, {
                        "tokens": yuan_to_tokens(float(body.get("convert_yuan_to_tokens") or 0)),
                        "pricing": load_pricing(),
                    })
                    return True
                if body.get("convert_tokens_to_yuan") is not None:
                    self._json(200, {
                        "yuan": tokens_to_yuan(int(body.get("convert_tokens_to_yuan") or 0)),
                        "display": format_cny(tokens_to_yuan(int(body.get("convert_tokens_to_yuan") or 0))),
                        "pricing": load_pricing(),
                    })
                    return True
                saved = save_pricing(body)
                self._json(200, {"pricing": saved, "usage": usage_totals()})
                return True

            # ---- Codex Responses bridge (/v1/*) → MiMo chat.completions
            if path.startswith("/v1/"):
                return self._handle_v1(path)

            self._json(404, {"error": f"unknown route {self.command} {path}"})
            return True
        except Exception as e:  # never crash the server
            self._json(500, {"error": str(e)})
            return True

    def _auth_bearer(self) -> str:
        raw = self.headers.get("Authorization") or ""
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return ""

    def _upstream(self) -> tuple[str, str]:
        """(base_url, token) for MiMo Desktop chat.completions."""
        base = _PREPARED.get("base_url") or ""
        if _PREPARED.get("live_port"):
            base = f"http://127.0.0.1:{int(_PREPARED['live_port'])}/v1"
        token = _PREPARED.get("token") or get_plain_token() or ""
        if not base or not token:
            pids = mimo_pids()
            tok = read_env_token()
            if tok and pids:
                port, _ = find_endpoint(tok, pids)
                if port:
                    base = f"http://127.0.0.1:{port}/v1"
                    token = tok
        return base.rstrip("/"), token

    def _authorize_and_track(self) -> tuple[bool, str, dict | None]:
        """Managed key check. Returns (ok, err, key_or_None)."""
        presented = self._auth_bearer()
        if not presented or not presented.startswith("mlk_"):
            # raw MiMo token or missing — allow (legacy path, not metered)
            return True, "", None
        allowed, reason, key = check_quota(presented)
        if not allowed:
            return False, reason or "forbidden", None
        return True, "", key

    def _track_usage(self, key: dict | None, chat: dict | None) -> None:
        if not key or not isinstance(chat, dict):
            return
        inp, out, _ = extract_usage(chat.get("usage"))
        if inp or out:
            record_usage(key["id"], inp, out)

    def _track_usage_stream(self, key: dict | None, chunks: list[dict]) -> None:
        if not key:
            return
        # Prefer the authoritative usage object (typically the final chunk).
        # Never mix it with per-delta estimates — that double-counts output.
        real = (0, 0, 0)
        est_out = 0
        for ch in chunks or []:
            usage = (ch or {}).get("usage")
            if usage:
                parsed = extract_usage(usage)
                if any(parsed):
                    real = parsed
                continue
            # rough fallback only when upstream omits usage entirely
            delta = ((ch or {}).get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                est_out += max(1, len(str(delta["content"])) // 4)
        inp, out = (real[0], real[1]) if any(real) else (0, est_out)
        if inp or out:
            record_usage(key["id"], inp, out)

    def _handle_v1(self, path: str) -> bool:
        if path == "/v1/models" and self.command == "GET":
            base, token = self._upstream()
            import urllib.request
            req = urllib.request.Request(base + "/models")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    body = r.read()
                # Hide non-public provider models from external discovery.
                try:
                    payload = json.loads(body.decode("utf-8"))
                    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                        payload["data"] = [m for m in payload["data"]
                                           if not is_hidden_model((m or {}).get("id") if isinstance(m, dict) else m)]
                        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json(502, {"error": f"upstream models failed: {e}"})
            return True

        if path == "/v1/responses" and self.command == "POST":
            ok, err, key = self._authorize_and_track()
            if not ok:
                code = 429 if "quota" in err else 401
                self._json(code, {"error": err})
                return True
            body = self._read_body()
            if is_hidden_model(body.get("model")):
                self._json(404, {"error": {"code": "model_not_found", "message": "model not found"}})
                return True
            base, token = self._upstream()
            if not base:
                self._json(503, {"error": "MiMo Desktop endpoint not ready — open MiMo Link and run 同步端点"})
                return True
            stream = bool(body.get("stream"))
            chat_req = responses_to_chat(body)

            if not stream:
                status, chat, raw = call_mimo_chat(chat_req, base, token)
                if status != 200 or not chat:
                    self._json(status or 502, {"error": raw or "upstream error"})
                    return True
                self._track_usage(key, chat)
                self._json(200, chat_to_responses(chat, body))
                return True

            # SSE stream (Codex default). Collect upstream deltas then emit Responses events.
            chunks: list[dict] = []
            try:
                for ch in call_mimo_chat_stream(chat_req, base, token):
                    chunks.append(ch)
            except Exception as e:
                self._json(502, {"error": f"upstream stream failed: {e}"})
                return True

            self._track_usage_stream(key, chunks)
            events = chat_chunks_to_response_events(chunks, body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for name, data in events:
                payload = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                self.wfile.write(payload.encode("utf-8"))
            return True

        # Chat Completions proxy (Hermes / OpenAI-compatible clients) with metering
        if path == "/v1/chat/completions" and self.command == "POST":
            ok, err, key = self._authorize_and_track()
            if not ok:
                code = 429 if "quota" in err else 401
                self._json(code, {"error": err})
                return True
            body = self._read_body()
            if is_hidden_model(body.get("model")):
                self._json(404, {"error": {"code": "model_not_found", "message": "model not found"}})
                return True
            base, token = self._upstream()
            if not base:
                self._json(503, {"error": "MiMo Desktop endpoint not ready"})
                return True
            if body.get("stream"):
                chunks = []
                try:
                    for ch in call_mimo_chat_stream(body, base, token):
                        chunks.append(ch)
                except Exception as e:
                    self._json(502, {"error": str(e)})
                    return True
                self._track_usage_stream(key, chunks)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for ch in chunks:
                    self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                return True
            status, chat, raw = call_mimo_chat(body, base, token)
            if status != 200 or not chat:
                self._json(status or 502, {"error": raw or "upstream error"})
                return True
            self._track_usage(key, chat)
            self._json(200, chat)
            return True

        self._json(404, {"error": "Not Found"})
        return True

    def do_GET(self) -> None:
        if self._handle_api():
            return
        if urlparse(self.path).path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        if self._handle_api():
            return
        self._json(404, {"error": "not found"})

    def do_PATCH(self) -> None:
        if self._handle_api():
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if self._handle_api():
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if self._handle_api():
            return
        self._json(404, {"error": "not found"})


def serve(port: int, open_browser: bool) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"MiMo Link running at {url}")
    print("Ctrl+C to quit.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


def main() -> int:
    ap = argparse.ArgumentParser(description="MiMo Link app")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open browser on start")
    args = ap.parse_args()
    if os.name != "nt":
        print("warning: mimo_link_core currently targets Windows", file=sys.stderr)
    serve(args.port, args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
