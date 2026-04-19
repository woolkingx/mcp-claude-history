from __future__ import annotations

from typing import Any


def _field_map(candidate: dict[str, Any]) -> dict[str, str]:
    return {
        "user_text": str(candidate.get("user_text", "") or ""),
        "assist_text": str(candidate.get("assist_text", "") or ""),
        "tool_names": str(candidate.get("tool_names", "") or ""),
        "tool_input": str(candidate.get("tool_input", "") or ""),
        "tool_result": str(candidate.get("tool_result", "") or ""),
    }


def snippet(text: str, token_set: set[str], max_lines: int = 3) -> str:
    """Show full matched lines with token highlighting, grep-style.

    Multiple tokens on the same line are merged into one output line.
    Returns at most ``max_lines`` matched lines.
    """
    lowered_tokens = {t.lower() for t in token_set}
    lines = text.split("\n")
    matched: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if not any(t in low for t in lowered_tokens):
            continue
        # Bold all token occurrences in the line.
        result = stripped
        for token in sorted(token_set, key=len, reverse=True):
            pos = 0
            parts: list[str] = []
            rlow = result.lower()
            while pos < len(rlow):
                idx = rlow.find(token.lower(), pos)
                if idx == -1:
                    parts.append(result[pos:])
                    break
                parts.append(result[pos:idx])
                parts.append(f"**{result[idx:idx + len(token)]}**")
                pos = idx + len(token)
                rlow = result.lower()
            result = "".join(parts)
        matched.append(result)
        if len(matched) >= max_lines:
            break

    if matched:
        # Merge adjacent bold markers: **a****b** → **ab**
        return "\n    ".join(m.replace("****", "") for m in matched)
    flat = text.replace("\n", " ").strip()
    return flat[:160] if len(flat) > 160 else flat


def analyze_candidate(
    candidate: dict[str, Any],
    query_tokens: list[str],
    search_fields: set[str],
) -> tuple[int, float, list[dict[str, Any]]]:
    field_map = _field_map(candidate)
    total_mc = 0
    phrase_bonus = 0.0
    matched_fields: list[dict[str, Any]] = []
    token_set = set(query_tokens)
    phrase = " ".join(query_tokens)

    for field_name in search_fields:
        text = field_map[field_name]
        if not text:
            continue
        lowered = text.lower()
        hits = sum(1 for token in query_tokens if token in lowered)
        if hits <= 0:
            continue
        total_mc += hits
        matched_fields.append(
            {
                "field": field_name,
                "hits": hits,
                "snippet": snippet(text, token_set),
            }
        )
        if phrase and phrase in lowered:
            phrase_bonus += float(len(query_tokens))

    return total_mc, phrase_bonus, matched_fields


def score_from_analysis(
    candidate: dict[str, Any],
    analysis: tuple[int, float, list[dict[str, Any]]],
    *,
    now: float | None = None,
) -> float:
    total_mc, phrase_bonus, _ = analysis
    if total_mc <= 0:
        return 0.0

    candidate_now = float(now if now is not None else candidate.get("mtime", candidate.get("session_mtime", 0.0)))
    mtime = float(candidate.get("mtime", candidate.get("session_mtime", candidate_now)))
    age_days = max((candidate_now - mtime) / 86400, 0.01)
    recency = 1.0 / (1.0 + age_days)
    return (total_mc + phrase_bonus) * (1.0 + recency)
