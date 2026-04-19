import re
from typing import Any

import orjson

from mcp_claude_history.jsonl_schema import CONTENT_BEARING_TYPES
from mcp_claude_history.schema import NormalizedEntry, SearchableFields

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _strip_system_reminder(text: str) -> str:
    return " ".join(_SYSTEM_REMINDER_RE.sub("", text).split())


def _text_from_block_list(blocks: Any) -> tuple[str, list[str], list[str], list[str]]:
    texts: list[str] = []
    tool_names: list[str] = []
    tool_inputs: list[str] = []
    tool_results: list[str] = []

    if not isinstance(blocks, list):
        return "", tool_names, tool_inputs, tool_results

    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                texts.append(text)
        elif block_type == "tool_use":
            name = block.get("name", "")
            if isinstance(name, str) and name:
                tool_names.append(name)
            tool_input = block.get("input")
            if tool_input is not None:
                tool_inputs.append(orjson.dumps(tool_input).decode())
        elif block_type == "tool_result":
            content = block.get("content", "")
            if isinstance(content, str):
                tool_results.append(content[:500])
            elif isinstance(content, list):
                for sub_block in content:
                    if isinstance(sub_block, dict) and sub_block.get("type") == "text":
                        text = sub_block.get("text", "")
                        if isinstance(text, str):
                            tool_results.append(text[:500])

    return " ".join(texts).strip(), tool_names, tool_inputs, tool_results


def extract_normalized_entry(
    entry: dict[str, Any],
    *,
    file_path: str,
    line_num: int,
    project: str,
) -> NormalizedEntry | None:
    msg_type = entry.get("type")
    if msg_type not in CONTENT_BEARING_TYPES:
        return None

    message = entry.get("message")
    if not isinstance(message, dict):
        return None

    model = message.get("model", "")
    if not isinstance(model, str):
        model = ""

    cwd = entry.get("cwd", "")
    if not isinstance(cwd, str):
        cwd = ""

    timestamp = entry.get("timestamp", "")
    if not isinstance(timestamp, str):
        timestamp = ""

    content = message.get("content", "")
    content_types: set[str] = set()
    user_text = ""
    assist_text = ""
    tool_names: list[str] = []
    tool_inputs: list[str] = []
    tool_results: list[str] = []

    if msg_type == "user":
        if isinstance(content, str):
            user_text = _strip_system_reminder(content)
            content_types.add("text")
        elif isinstance(content, list):
            user_text, tool_names, tool_inputs, tool_results = _text_from_block_list(content)
            user_text = _strip_system_reminder(user_text)
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("type"), str):
                    content_types.add(block["type"])
        else:
            return None
    else:
        if not isinstance(content, list):
            return None
        assist_text, tool_names, tool_inputs, tool_results = _text_from_block_list(content)
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("type"), str):
                content_types.add(block["type"])

    searchable = SearchableFields(
        user_text=user_text,
        assist_text=assist_text,
        tool_names=", ".join(tool_names),
        tool_input=" | ".join(tool_inputs),
        tool_result=" ".join(tool_results),
    )
    content_type = "mixed" if len(content_types) > 1 else (next(iter(content_types)) if content_types else "")

    return NormalizedEntry(
        msg_type=msg_type,
        content_type=content_type,
        cwd=cwd,
        model=model,
        timestamp=timestamp,
        project=project,
        line_num=line_num,
        file_path=file_path,
        searchable=searchable,
    )
