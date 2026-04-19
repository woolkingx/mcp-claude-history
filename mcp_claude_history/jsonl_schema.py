"""Upstream JSONL transcript schema — mirrors Claude Code's session log format.

Source of truth: claude-code/types/logs.ts (TranscriptMessage, Entry union)
Session files:   ~/.claude/projects/<project-hash>/<session-id>.jsonl

Each line is a JSON object discriminated by ``type``.  The three
content-bearing types (user / assistant / system) carry a ``message``
dict whose shape follows the Anthropic Messages API.  All other types
are session metadata (titles, tags, attribution, context-collapse, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Content blocks inside message.content ─────────────────────────

@dataclass(frozen=True, slots=True)
class TextBlock:
    type: str = "text"          # literal "text"
    text: str = ""


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    type: str = "thinking"      # literal "thinking"
    thinking: str = ""
    signature: str = ""


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    type: str = "tool_use"      # literal "tool_use"
    id: str = ""                # tool_use_id
    name: str = ""              # tool name (Bash, Edit, Read, …)
    input: dict[str, Any] = field(default_factory=dict)
    caller: str | None = None   # optional caller context


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    type: str = "tool_result"   # literal "tool_result"
    tool_use_id: str = ""
    content: str | list[dict[str, Any]] = ""  # str or list of sub-blocks


# Union of all content block shapes.
ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


# ── message.usage ────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MessageUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


# ── message (Anthropic API Message subset) ───────────────────────

@dataclass(frozen=True, slots=True)
class ApiMessage:
    """The ``message`` field inside a TranscriptEntry.

    For user entries: ``role="user"``, content is ``str`` or
    ``list[ContentBlock]``.
    For assistant entries: ``role="assistant"``, content is always
    ``list[ContentBlock]``.
    """
    role: str = ""                              # "user" | "assistant"
    content: str | list[dict[str, Any]] = ""    # str or ContentBlock[]
    # assistant-only fields
    id: str | None = None                       # API message ID
    model: str | None = None                    # e.g. "claude-opus-4-20250514"
    stop_reason: str | None = None
    stop_sequence: str | None = None
    stop_details: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None         # MessageUsage shape


# ── TranscriptEntry (type: user | assistant | system) ────────────

@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """Main content-bearing entry.  Indexed by mcp-claude-history."""
    # discriminator
    type: str = ""              # "user" | "assistant" | "system"

    # session envelope (SerializedMessage)
    uuid: str = ""              # message UUID
    sessionId: str = ""         # session UUID
    cwd: str = ""
    userType: str = ""          # user type identifier
    entrypoint: str = ""        # "cli" | "sdk-ts" | "sdk-py" | …
    timestamp: str = ""         # ISO 8601
    version: str = ""           # Claude Code version string
    gitBranch: str = ""
    slug: str = ""              # session slug (plan files, etc.)

    # transcript chain
    parentUuid: str | None = None
    logicalParentUuid: str | None = None
    isSidechain: bool = False

    # agent / team context
    agentId: str | None = None
    teamName: str | None = None
    agentName: str | None = None
    agentColor: str | None = None
    promptId: str | None = None

    # assistant-only
    requestId: str | None = None

    # user-only
    permissionMode: str | None = None
    sourceToolAssistantUUID: str | None = None
    sourceToolUseID: str | None = None
    toolUseResult: Any = None
    isMeta: bool | None = None

    # message body — Anthropic API Message
    message: dict[str, Any] = field(default_factory=dict)

    # system-only
    subtype: str | None = None
    level: str | None = None
    stopReason: str | None = None
    hasOutput: bool | None = None
    hookCount: int | None = None
    hookErrors: Any = None
    hookInfos: Any = None
    preventedContinuation: bool | None = None
    toolUseID: str | None = None


# ── AttachmentEntry (type: attachment) ───────────────────────────

@dataclass(frozen=True, slots=True)
class AttachmentData:
    type: str = ""              # "hook_success" | "hook_error" | …
    command: str = ""
    hookName: str = ""
    hookEvent: str = ""
    stdout: str = ""
    stderr: str = ""
    content: str = ""
    exitCode: int | None = None
    durationMs: int | None = None
    toolUseID: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentEntry:
    """Hook results, system-reminders, and other side-channel data."""
    type: str = "attachment"
    uuid: str = ""
    sessionId: str = ""
    cwd: str = ""
    timestamp: str = ""
    version: str = ""
    gitBranch: str = ""
    parentUuid: str | None = None
    isSidechain: bool = False
    userType: str = ""
    entrypoint: str = ""
    attachment: dict[str, Any] = field(default_factory=dict)


# ── Metadata entries (no message field) ──────────────────────────

@dataclass(frozen=True, slots=True)
class PermissionModeEntry:
    type: str = "permission-mode"
    permissionMode: str = ""
    sessionId: str = ""


@dataclass(frozen=True, slots=True)
class LastPromptEntry:
    type: str = "last-prompt"
    sessionId: str = ""
    lastPrompt: str = ""


@dataclass(frozen=True, slots=True)
class CustomTitleEntry:
    type: str = "custom-title"
    sessionId: str = ""
    customTitle: str = ""


@dataclass(frozen=True, slots=True)
class AiTitleEntry:
    type: str = "ai-title"
    sessionId: str = ""
    aiTitle: str = ""


@dataclass(frozen=True, slots=True)
class SummaryEntry:
    type: str = "summary"
    leafUuid: str = ""
    summary: str = ""


@dataclass(frozen=True, slots=True)
class TaskSummaryEntry:
    type: str = "task-summary"
    sessionId: str = ""
    summary: str = ""
    timestamp: str = ""


@dataclass(frozen=True, slots=True)
class TagEntry:
    type: str = "tag"
    sessionId: str = ""
    tag: str = ""


@dataclass(frozen=True, slots=True)
class AgentNameEntry:
    type: str = "agent-name"
    sessionId: str = ""
    agentName: str = ""


@dataclass(frozen=True, slots=True)
class AgentColorEntry:
    type: str = "agent-color"
    sessionId: str = ""
    agentColor: str = ""


@dataclass(frozen=True, slots=True)
class AgentSettingEntry:
    type: str = "agent-setting"
    sessionId: str = ""
    agentSetting: str = ""


@dataclass(frozen=True, slots=True)
class PRLinkEntry:
    type: str = "pr-link"
    sessionId: str = ""
    prNumber: int = 0
    prUrl: str = ""
    prRepository: str = ""
    timestamp: str = ""


@dataclass(frozen=True, slots=True)
class ModeEntry:
    type: str = "mode"
    sessionId: str = ""
    mode: str = ""              # "coordinator" | "normal"


@dataclass(frozen=True, slots=True)
class WorktreeStateEntry:
    type: str = "worktree-state"
    sessionId: str = ""
    worktreeSession: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FileHistorySnapshotEntry:
    type: str = "file-history-snapshot"
    messageId: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    isSnapshotUpdate: bool = False


@dataclass(frozen=True, slots=True)
class AttributionSnapshotEntry:
    type: str = "attribution-snapshot"
    messageId: str = ""
    surface: str = ""
    fileStates: dict[str, Any] = field(default_factory=dict)
    promptCount: int | None = None
    promptCountAtLastCommit: int | None = None


@dataclass(frozen=True, slots=True)
class ContentReplacementEntry:
    type: str = "content-replacement"
    sessionId: str = ""
    agentId: str | None = None
    replacements: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ContextCollapseCommitEntry:
    type: str = "marble-origami-commit"
    sessionId: str = ""
    collapseId: str = ""
    summaryUuid: str = ""
    summaryContent: str = ""
    summary: str = ""
    firstArchivedUuid: str = ""
    lastArchivedUuid: str = ""


@dataclass(frozen=True, slots=True)
class ContextCollapseSnapshotEntry:
    type: str = "marble-origami-snapshot"
    sessionId: str = ""
    staged: list[dict[str, Any]] = field(default_factory=list)
    armed: bool = False
    lastSpawnTokens: int = 0


@dataclass(frozen=True, slots=True)
class SpeculationAcceptEntry:
    type: str = "speculation-accept"
    timestamp: str = ""
    timeSavedMs: int = 0


# ── Entry type discriminator ────────────────────────────────────

# All possible type values and their corresponding dataclass.
ENTRY_TYPE_MAP: dict[str, type] = {
    "user": TranscriptEntry,
    "assistant": TranscriptEntry,
    "system": TranscriptEntry,
    "attachment": AttachmentEntry,
    "permission-mode": PermissionModeEntry,
    "last-prompt": LastPromptEntry,
    "custom-title": CustomTitleEntry,
    "ai-title": AiTitleEntry,
    "summary": SummaryEntry,
    "task-summary": TaskSummaryEntry,
    "tag": TagEntry,
    "agent-name": AgentNameEntry,
    "agent-color": AgentColorEntry,
    "agent-setting": AgentSettingEntry,
    "pr-link": PRLinkEntry,
    "mode": ModeEntry,
    "worktree-state": WorktreeStateEntry,
    "file-history-snapshot": FileHistorySnapshotEntry,
    "attribution-snapshot": AttributionSnapshotEntry,
    "content-replacement": ContentReplacementEntry,
    "marble-origami-commit": ContextCollapseCommitEntry,
    "marble-origami-snapshot": ContextCollapseSnapshotEntry,
    "speculation-accept": SpeculationAcceptEntry,
}

# Entry types that carry searchable conversation content.
CONTENT_BEARING_TYPES = frozenset({"user", "assistant"})

# Entry types that carry session-level metadata.
METADATA_TYPES = frozenset(ENTRY_TYPE_MAP.keys()) - CONTENT_BEARING_TYPES - {"system", "attachment"}
