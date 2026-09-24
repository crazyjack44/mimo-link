"""Tool schema — what the LLM sees."""

MIMO_LINK = {
    "name": "mimo_link",
    "description": (
        "Manage the link between Xiaomi MiMo Desktop's local capability API and Hermes. "
        "MiMo Desktop serves its session's chat models (the account's MiMo quota) over an "
        "OpenAI-compatible /v1 on a loopback port that CHANGES every desktop session; this tool "
        "finds the live port, ensures a scoped token, and syncs the `mimo-desktop` model alias. "
        "Use action=sync when the user wants to use MiMo Desktop quota in Hermes or after the "
        "MiMo Desktop app restarted and the model stopped answering; action=status reports the "
        "link state; action=models lists the models the desktop session can serve."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["sync", "status", "models", "route", "export"],
                "description": (
                    "sync = discover port/token/models (no Hermes write); "
                    "route = write Hermes mimo-desktop alias (explicit apply); "
                    "export = CC Switch AppConfig JSON; "
                    "status = report only; models = list reachable models"
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional model id for the alias, e.g. 'mimo-desktop/mimo-pro' (only with action=sync)",
            },
            "target": {
                "type": "string",
                "enum": ["hermes", "codex"],
                "description": "route/export target (default hermes). codex merge-writes ~/.codex/config.toml + auth.json",
            },
        },
        "required": ["action"],
    },
}
