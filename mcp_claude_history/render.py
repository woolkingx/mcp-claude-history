from __future__ import annotations

from mcp_claude_history.query import DEFAULT_FIELDS
from mcp_claude_history.schema import SearchResult


def format_search_result(result: SearchResult) -> str:
    search_fields = set(result.search_fields)
    out = f"{result.total_matches} matched, {len(result.sessions)} sessions"
    default_fields = ",".join(sorted(DEFAULT_FIELDS))
    current_fields = ",".join(sorted(search_fields))
    if current_fields != default_fields:
        out += f" | fields: {current_fields}"

    tags = [f"{k}={v}" for k, v in result.filters.items() if v]
    if tags:
        out += f" | {', '.join(tags)}"
    out += "\n"

    for index, session in enumerate(result.sessions, 1):
        out += f"\n## Session {index} — {session['file']}\n"
        out += f"project: {session['project']} | score: {session['session_score']:.1f} | hits: {session['total_hits']}\n\n"
        for message in session["messages"][:5]:
            role = message["msg_type"]
            tool = message.get("tool_names", "")
            label = f"[{role}]"
            if tool:
                label += f" {tool}"
            out += f"  L{message['line_num']} {label}\n"
            for matched_field in message["matched_fields"]:
                out += f"    {matched_field['snippet']}\n"
        remaining = len(session["messages"]) - 5
        if remaining > 0:
            out += f"  +{remaining} more\n"

    return out
