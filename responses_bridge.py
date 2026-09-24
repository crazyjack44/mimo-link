"""Codex Responses API ⇄ MiMo Chat Completions bridge.

Codex (wire_api=responses) only speaks POST /v1/responses.
MiMo Desktop only speaks /v1/chat/completions. This module converts both ways,
including a minimal SSE stream for Codex's stream=true default.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid


def _now() -> int:
    return int(time.time())


def _rid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# --------------------------------------------------------------------- request

def responses_to_chat(body: dict) -> dict:
    """Map a Responses API request body onto Chat Completions."""
    instructions = body.get("instructions") or ""
    messages: list[dict] = []

    if instructions:
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input", [])
    if isinstance(raw_input, str):
        raw_input = [{"role": "user", "content": raw_input}]

    pending_assistant: dict | None = None

    def flush_assistant() -> None:
        nonlocal pending_assistant
        if pending_assistant:
            messages.append(pending_assistant)
            pending_assistant = None

    for item in raw_input or []:
        if isinstance(item, str):
            flush_assistant()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type") or item.get("role") or ""

        if itype in ("message", "user", "assistant") or item.get("role") in ("user", "assistant", "system"):
            role = item.get("role") or ("user" if itype == "message" and item.get("role") != "assistant" else itype)
            if role not in ("user", "assistant", "system"):
                role = "user"
            parts = item.get("content")
            texts: list[str] = []
            if isinstance(parts, str):
                texts.append(parts)
            elif isinstance(parts, list):
                for p in parts:
                    if isinstance(p, str):
                        texts.append(p)
                    elif isinstance(p, dict):
                        if p.get("type") in ("input_text", "output_text", "text") and p.get("text"):
                            texts.append(str(p["text"]))
                        elif p.get("text") and p.get("type") in (None, "input_text"):
                            texts.append(str(p["text"]))
            content = "\n".join(t for t in texts if t)
            if role == "system":
                messages.append({"role": "system", "content": content})
            elif role == "assistant":
                flush_assistant()
                messages.append({"role": "assistant", "content": content})
            else:
                flush_assistant()
                messages.append({"role": "user", "content": content})
            continue

        if itype == "function_call":
            flush_assistant()
            call_id = item.get("call_id") or item.get("id") or _rid("call")
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }],
            })
            continue

        if itype == "function_call_output":
            flush_assistant()
            out = item.get("output")
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": str(out if out is not None else ""),
            })
            continue

        # fallback: treat as user text
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        if text:
            flush_assistant()
            messages.append({"role": "user", "content": str(text)})

    flush_assistant()

    out: dict = {
        "model": body.get("model") or "mimo-desktop/mimo-v2.6-pro",
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for k in ("temperature", "top_p", "max_output_tokens", "max_tokens"):
        if body.get(k) is not None:
            key = "max_tokens" if k in ("max_output_tokens", "max_tokens") else k
            out[key] = body[k]

    tools = body.get("tools") or []
    chat_tools = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" or t.get("name"):
            fn = {
                "name": t.get("name") or t.get("function", {}).get("name", ""),
                "description": t.get("description") or t.get("function", {}).get("description", ""),
                "parameters": t.get("parameters") or t.get("function", {}).get("parameters") or {"type": "object", "properties": {}},
            }
            chat_tools.append({"type": "function", "function": fn})
    if chat_tools:
        out["tools"] = chat_tools

    tool_choice = body.get("tool_choice")
    if tool_choice:
        out["tool_choice"] = tool_choice

    return out


# --------------------------------------------------------------------- response

def _assistant_text_and_tools(message: dict) -> tuple[str, list[dict]]:
    content = message.get("content")
    if isinstance(content, list):
        texts = []
        for p in content:
            if isinstance(p, dict) and p.get("text"):
                texts.append(p["text"])
            elif isinstance(p, str):
                texts.append(p)
        text = "\n".join(texts)
    else:
        text = content or ""
        if text is None:
            text = ""
    tools = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tools.append({
            "type": "function_call",
            "id": _rid("fc"),
            "call_id": tc.get("id") or _rid("call"),
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "{}",
        })
    return text, tools


def chat_to_responses(chat: dict, body: dict) -> dict:
    """Map a Chat Completions response onto a Responses API response object."""
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text, tool_items = _assistant_text_and_tools(message)

    output = []
    msg_id = _rid("msg")
    content_parts = []
    if text:
        content_parts.append({"type": "output_text", "text": text, "annotations": []})
    output.append({
        "type": "message",
        "id": msg_id,
        "status": "completed",
        "role": "assistant",
        "content": content_parts,
    })
    output.extend(tool_items)

    usage = chat.get("usage") or {}
    return {
        "id": _rid("resp"),
        "object": "response",
        "created_at": chat.get("created") or _now(),
        "status": "completed",
        "background": False,
        "model": body.get("model") or chat.get("model") or "",
        "output": output,
        "parallel_tool_calls": True,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
            "total_tokens": usage.get("total_tokens") or 0,
        },
        "error": None,
        "incomplete_details": None,
    }


def chat_chunks_to_response_events(chunks: list[dict], body: dict) -> list[tuple[str, dict]]:
    """Fold streamed chat chunks into Responses SSE events (non-tool path + tools)."""
    resp_id = _rid("resp")
    msg_id = _rid("msg")
    text_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    model = body.get("model") or ""
    finish_reason = "stop"

    for ch in chunks:
        if ch.get("model"):
            model = ch.get("model") or model
        choice = (ch.get("choices") or [{}])[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            text_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index") or 0
            acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                acc["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc["name"] += fn["name"]
            if fn.get("arguments"):
                acc["arguments"] += fn["arguments"]

    text = "".join(text_parts)
    created = _now()
    shell = {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "background": False,
        "model": model,
        "output": [],
        "parallel_tool_calls": True,
        "usage": None,
        "error": None,
        "incomplete_details": None,
    }

    def ev(name: str, data: dict) -> tuple[str, dict]:
        payload = {"type": name, **data}
        return name, payload

    events: list[tuple[str, dict]] = [
        ev("response.created", {"response": dict(shell)}),
    ]

    if text or not tool_acc:
        events.append(ev("response.output_item.added", {
            "output_index": 0,
            "item": {"type": "message", "id": msg_id, "status": "in_progress", "role": "assistant", "content": []},
        }))
        events.append(ev("response.content_part.added", {
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        }))
        if text:
            events.append(ev("response.output_text.delta", {
                "item_id": msg_id, "output_index": 0, "content_index": 0, "delta": text,
            }))
        events.append(ev("response.output_text.done", {
            "item_id": msg_id, "output_index": 0, "content_index": 0, "text": text,
        }))
        events.append(ev("response.content_part.done", {
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        }))
        msg_item = {
            "type": "message", "id": msg_id, "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}] if text else [],
        }
        events.append(ev("response.output_item.done", {"output_index": 0, "item": msg_item}))
        shell["output"].append(msg_item)

    oidx = 1 if (text or not tool_acc) else 0
    for _, acc in sorted(tool_acc.items()):
        fc_id = _rid("fc")
        call_id = acc["id"] or _rid("call")
        item = {
            "type": "function_call",
            "id": fc_id,
            "call_id": call_id,
            "name": acc["name"],
            "arguments": acc["arguments"] or "{}",
            "status": "completed",
        }
        events.append(ev("response.output_item.added", {
            "output_index": oidx,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id,
                     "name": acc["name"], "arguments": "", "status": "in_progress"},
        }))
        events.append(ev("response.function_call_arguments.delta", {
            "item_id": fc_id, "output_index": oidx, "delta": item["arguments"],
        }))
        events.append(ev("response.function_call_arguments.done", {
            "item_id": fc_id, "output_index": oidx, "name": item["name"], "arguments": item["arguments"],
        }))
        events.append(ev("response.output_item.done", {"output_index": oidx, "item": item}))
        shell["output"].append(item)
        oidx += 1

    final = dict(shell)
    final["status"] = "completed"
    if finish_reason == "length":
        final["status"] = "incomplete"
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    events.append(ev("response.completed", {"response": final}))
    return events


# --------------------------------------------------------------------- upstream

def call_mimo_chat(chat_body: dict, upstream_base: str, token: str, timeout: float = 120.0):
    """POST chat.completions to MiMo. Returns (status, parsed_json_or_none, raw_text)."""
    url = upstream_base.rstrip("/") + "/chat/completions"
    data = json.dumps(chat_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), raw
            except Exception:
                return r.status, None, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), raw
        except Exception:
            return e.code, None, raw
    except Exception as e:
        return 0, None, str(e)


def call_mimo_chat_stream(chat_body: dict, upstream_base: str, token: str, timeout: float = 120.0):
    """Yield chat.completion.chunk JSON objects from upstream SSE."""
    url = upstream_base.rstrip("/") + "/chat/completions"
    body = dict(chat_body)
    body["stream"] = True
    # Ask upstream for a final usage object so metering is accurate
    so = body.get("stream_options") or {}
    if isinstance(so, dict):
        so["include_usage"] = True
        body["stream_options"] = so
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield json.loads(payload)
                    except Exception:
                        continue


def handle_responses(body: dict, upstream_base: str, token: str) -> dict:
    """Non-stream path: Responses request → chat → Responses object."""
    chat_body = responses_to_chat(body)
    chat_body["stream"] = False
    status, chat, raw = call_mimo_chat(chat_body, upstream_base, token)
    if status != 200 or not chat:
        return {"__error__": status or 502, "__raw__": raw}
    return chat_to_responses(chat, body)
