"""mimo-link — use Xiaomi MiMo Desktop's quota in Hermes via its capability API."""

import json
import logging

from . import schemas
from .mimo_link_core import run_action, DEFAULT_MODEL

logger = logging.getLogger(__name__)

_ACTIONS = ("sync", "status", "models", "route", "export")


def _tool_handler(args: dict, **kwargs) -> str:
    """Tool handler: args dict in, JSON string out — never raises."""
    try:
        action = str(args.get("action", "status"))
        model = args.get("model")
        target = str(args.get("target") or "hermes")
        report = run_action(action, model=str(model) if model else None, target=target)
    except Exception as e:
        logger.exception("mimo_link failed")
        report = {"error": str(e)}
    return json.dumps(report, ensure_ascii=False)


def _slash_handler(raw: str) -> str:
    """/mimo-link [sync|status|models|route|export] [model-id] [--target hermes|codex]"""
    parts = raw.split()
    action = parts[0] if parts and parts[0] in _ACTIONS else "status"
    model = parts[1] if len(parts) > 1 and not parts[1].startswith("-") else None
    target = "hermes"
    if "--target" in parts:
        i = parts.index("--target")
        if i + 1 < len(parts):
            target = parts[i + 1]
    try:
        report = run_action(action, model=model, target=target)
    except Exception as e:
        logger.exception("mimo-link failed")
        report = {"error": str(e)}
    lines = [f"{k}: {v}" for k, v in report.items() if k not in ("models", "config", "codex")]
    if report.get("models"):
        lines.append("models: " + ", ".join(report["models"]))
    return "\n".join(lines)


def _cli_setup(subparser) -> None:
    subparser.add_argument("action", nargs="?", default="status", choices=list(_ACTIONS),
                           help="sync = prepare endpoint; route = write target config; "
                                "export = CC Switch JSON; status = report; models = list models")
    subparser.add_argument("--model", help=f"alias model id (default: keep current / {DEFAULT_MODEL})")
    subparser.add_argument("--target", choices=["hermes", "codex"], default="hermes",
                           help="route/export target")


def _cli_handler(args) -> None:
    print(json.dumps(run_action(getattr(args, "action", "status"),
                                model=getattr(args, "model", None),
                                target=getattr(args, "target", "hermes")),
                     ensure_ascii=False, indent=2))


def register(ctx) -> None:
    ctx.register_tool(
        name="mimo_link",
        toolset="mimo-link",
        schema=schemas.MIMO_LINK,
        handler=_tool_handler,
        description=schemas.MIMO_LINK["description"],
        emoji="🔌",
    )
    ctx.register_command(
        "mimo-link",
        _slash_handler,
        description="Link MiMo Desktop quota into Hermes (sync|route|export|status|models)",
        args_hint="[action] [model]",
    )
    ctx.register_cli_command(
        "mimo-link",
        help="Link Xiaomi MiMo Desktop's capability API into Hermes",
        setup_fn=_cli_setup,
        handler_fn=_cli_handler,
        description="Keep the mimo-desktop model alias pointed at the live MiMo Desktop endpoint",
    )
