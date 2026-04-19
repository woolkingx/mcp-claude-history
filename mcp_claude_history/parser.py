from typing import Any

import orjson


def parse_json_line(raw: bytes) -> dict[str, Any] | None:
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = orjson.loads(stripped)
    except (orjson.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None

