from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SearchableFields:
    user_text: str = ""
    assist_text: str = ""
    tool_names: str = ""
    tool_input: str = ""
    tool_result: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedEntry:
    msg_type: str
    content_type: str
    cwd: str
    model: str
    timestamp: str
    project: str
    line_num: int
    file_path: str
    searchable: SearchableFields


@dataclass(frozen=True, slots=True)
class IndexSummary:
    new: int
    modified: int
    stale: int
    unchanged: int
    indexed: int
    appended: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    query: str
    search_fields: tuple[str, ...]
    filters: dict[str, str]
    total_matches: int
    sessions: list[dict[str, Any]]
