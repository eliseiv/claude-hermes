"""Tool schemas (CO-1): client-side iOS tools + server-side site.* tools, Pydantic v2.

Two classes (ADR-011):
- client-side (files.*/calendar.*/reminders.*): backend only INITIATES the tool-call; the iOS
  client executes it and posts a tool_result.
- server-side (site.*): backend EXECUTES the handler itself, in the same tool-loop, without a
  round-trip to iOS (SERVER_SIDE_TOOLS).

Mutating tools (files.write, files.mkdir, calendar.create_events, reminders.create,
site.write_file, site.delete) require an audit record. Args/result are strictly validated
(extra='forbid'); `path` rejects `..`-traversal.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Tool names (fixed list — validated at the API boundary).
TOOL_FILES_READ = "files.read"
TOOL_FILES_WRITE = "files.write"
TOOL_FILES_LIST = "files.list"
TOOL_FILES_MKDIR = "files.mkdir"
TOOL_CALENDAR_READ = "calendar.read"
TOOL_CALENDAR_CREATE = "calendar.create_events"
TOOL_REMINDERS_READ = "reminders.read"
TOOL_REMINDERS_CREATE = "reminders.create"

# Server-side tools (site.*, ADR-011): executed by the backend, not the iOS client.
TOOL_SITE_WRITE_FILE = "site.write_file"
TOOL_SITE_PREVIEW = "site.preview"
TOOL_SITE_LIST = "site.list"
TOOL_SITE_READ = "site.read"
TOOL_SITE_DELETE = "site.delete"

# Tools whose execution lives entirely on the backend (no status=tool_call to iOS, ADR-011 §1).
SERVER_SIDE_TOOLS = frozenset(
    {
        TOOL_SITE_WRITE_FILE,
        TOOL_SITE_PREVIEW,
        TOOL_SITE_LIST,
        TOOL_SITE_READ,
        TOOL_SITE_DELETE,
    }
)

ALL_TOOL_NAMES = frozenset(
    {
        TOOL_FILES_READ,
        TOOL_FILES_WRITE,
        TOOL_FILES_LIST,
        TOOL_FILES_MKDIR,
        TOOL_CALENDAR_READ,
        TOOL_CALENDAR_CREATE,
        TOOL_REMINDERS_READ,
        TOOL_REMINDERS_CREATE,
        *SERVER_SIDE_TOOLS,
    }
)

# BUG-3: Anthropic Messages API requires tool.name to match ^[a-zA-Z0-9_-]{1,128}$ — a dot is
# rejected with 400 (→ backend 502). The public iOS contract (TZ §5) uses dotted domain names and
# must NOT change. We therefore keep a static, bidirectional name map (13 fixed pairs, incl.
# server-side site.*) that is the single source of truth for name correspondence. It is applied
# ONLY at the Anthropic transport
# boundary: forward (domain→anthropic) when building tools[].name for messages.create, reverse
# (anthropic→domain) when parsing a tool_use block from Claude. Everywhere else — DB
# (tool_calls.tool_name), audit, API responses (toolCall.name), arg/result typing — stays domain.
_DOMAIN_TO_ANTHROPIC: dict[str, str] = {
    TOOL_FILES_READ: "files_read",
    TOOL_FILES_WRITE: "files_write",
    TOOL_FILES_LIST: "files_list",
    TOOL_FILES_MKDIR: "files_mkdir",
    TOOL_CALENDAR_READ: "calendar_read",
    TOOL_CALENDAR_CREATE: "calendar_create_events",
    TOOL_REMINDERS_READ: "reminders_read",
    TOOL_REMINDERS_CREATE: "reminders_create",
    # Server-side site.* (ADR-011 §3): same dot→underscore mapping as client-side tools.
    TOOL_SITE_WRITE_FILE: "site_write_file",
    TOOL_SITE_PREVIEW: "site_preview",
    TOOL_SITE_LIST: "site_list",
    TOOL_SITE_READ: "site_read",
    TOOL_SITE_DELETE: "site_delete",
}
_ANTHROPIC_TO_DOMAIN: dict[str, str] = {a: d for d, a in _DOMAIN_TO_ANTHROPIC.items()}


class UnknownToolNameError(Exception):
    """Claude returned a tool_use.name that is not in the static map (upstream anomaly).

    Treated as an upstream processing error, never forwarded to iOS as a valid tool name.
    """


def to_anthropic_tool_name(domain_name: str) -> str:
    """Forward map domain-name (dotted) → anthropic-name (underscore). Static table only."""
    anthropic_name = _DOMAIN_TO_ANTHROPIC.get(domain_name)
    if anthropic_name is None:
        raise UnknownToolNameError(f"unknown domain tool name: {domain_name}")
    return anthropic_name


def to_domain_tool_name(anthropic_name: str) -> str:
    """Reverse map anthropic-name (underscore) → domain-name (dotted). Static table only.

    Raises UnknownToolNameError if Claude returns a name absent from the map (upstream anomaly).
    """
    domain_name = _ANTHROPIC_TO_DOMAIN.get(anthropic_name)
    if domain_name is None:
        raise UnknownToolNameError(f"unknown anthropic tool name: {anthropic_name}")
    return domain_name


# Mutating tools require audit (AC-7; ADR-011 §4 adds site.write_file / site.delete).
MUTATING_TOOLS = frozenset(
    {
        TOOL_FILES_WRITE,
        TOOL_FILES_MKDIR,
        TOOL_CALENDAR_CREATE,
        TOOL_REMINDERS_CREATE,
        TOOL_SITE_WRITE_FILE,
        TOOL_SITE_DELETE,
    }
)


def _validate_safe_path(value: str) -> str:
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("path must not contain '..' traversal")
    return value


SafePath = Annotated[str, Field(min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PathModel(_StrictModel):
    path: SafePath

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_safe_path(value)


# --- files ---
class FilesReadArgs(_PathModel):
    pass


class FilesWriteArgs(_PathModel):
    content: str
    encoding: Literal["utf8", "base64"]
    overwrite: bool


class FilesListArgs(_PathModel):
    recursive: bool


class FilesMkdirArgs(_PathModel):
    createIntermediates: bool


# --- calendar ---
class CalendarReadArgs(_StrictModel):
    startDate: str
    endDate: str
    calendarId: str | None = None


class CalendarEventInput(_StrictModel):
    title: str
    start: str
    end: str
    location: str | None = None
    notes: str | None = None
    calendarId: str | None = None


class CalendarCreateArgs(_StrictModel):
    events: list[CalendarEventInput]


# --- reminders ---
class RemindersReadArgs(_StrictModel):
    listId: str | None = None
    includeCompleted: bool


class ReminderInput(_StrictModel):
    title: str
    due: str | None = None
    notes: str | None = None
    listId: str | None = None


class RemindersCreateArgs(_StrictModel):
    reminders: list[ReminderInput]


# --- server-side site.* (ADR-011) ---
# IMPORTANT (IDOR guard, website-builder/05-security.md): args carry ONLY file data. The owning
# userId and external_project_id come from the session context on the backend, NEVER from these
# args — so the model cannot target another user's project.
class SiteWriteFileArgs(_PathModel):
    content: str
    contentType: str
    encoding: Literal["utf8", "base64"]


class SitePreviewArgs(_StrictModel):
    entry: str | None = None


class SiteListArgs(_StrictModel):
    pass


class SiteReadArgs(_PathModel):
    pass


class SiteDeleteArgs(_PathModel):
    pass


_ARGS_BY_TOOL: dict[str, type[_StrictModel]] = {
    TOOL_FILES_READ: FilesReadArgs,
    TOOL_FILES_WRITE: FilesWriteArgs,
    TOOL_FILES_LIST: FilesListArgs,
    TOOL_FILES_MKDIR: FilesMkdirArgs,
    TOOL_CALENDAR_READ: CalendarReadArgs,
    TOOL_CALENDAR_CREATE: CalendarCreateArgs,
    TOOL_REMINDERS_READ: RemindersReadArgs,
    TOOL_REMINDERS_CREATE: RemindersCreateArgs,
    TOOL_SITE_WRITE_FILE: SiteWriteFileArgs,
    TOOL_SITE_PREVIEW: SitePreviewArgs,
    TOOL_SITE_LIST: SiteListArgs,
    TOOL_SITE_READ: SiteReadArgs,
    TOOL_SITE_DELETE: SiteDeleteArgs,
}


# Human-readable tool descriptions — single source of truth for both the Anthropic tool
# definitions and the GET /v1/tools catalog (ADR-019).
TOOL_DESCRIPTIONS: dict[str, str] = {
    TOOL_FILES_READ: "Read a file from the user's device.",
    TOOL_FILES_WRITE: "Write a file on the user's device.",
    TOOL_FILES_LIST: "List files/directories on the user's device.",
    TOOL_FILES_MKDIR: "Create a directory on the user's device.",
    TOOL_CALENDAR_READ: "Read calendar events in a date range.",
    TOOL_CALENDAR_CREATE: "Create calendar events.",
    TOOL_REMINDERS_READ: "Read reminders.",
    TOOL_REMINDERS_CREATE: "Create reminders.",
    TOOL_SITE_WRITE_FILE: (
        "Write or overwrite a file in the website project. Path is relative to the project "
        "root. Use encoding 'utf8' for text (HTML/CSS/JS) and 'base64' for binary assets "
        "(images/fonts). The project is the current chat session's project (no project id "
        "needed)."
    ),
    TOOL_SITE_PREVIEW: (
        "Get a temporary signed preview URL for the current website project. Optional 'entry' "
        "selects the start file (default index.html)."
    ),
    TOOL_SITE_LIST: "List the files of the current website project.",
    TOOL_SITE_READ: "Read a file from the current website project by relative path.",
    TOOL_SITE_DELETE: "Delete a file from the current website project by relative path.",
}


def validate_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate Claude-produced tool args against the strict schema. Raises ValueError."""
    model = _ARGS_BY_TOOL.get(tool_name)
    if model is None:
        raise ValueError(f"unknown tool: {tool_name}")
    return model.model_validate(args).model_dump()


def tool_input_schema(tool_name: str) -> dict[str, Any]:
    """JSON Schema of a tool's args (``model_json_schema()`` of its model), title stripped."""
    schema = _ARGS_BY_TOOL[tool_name].model_json_schema()
    schema.pop("title", None)
    return schema


def tool_catalog() -> list[dict[str, Any]]:
    """Machine-readable catalog of all backend tools for GET /v1/tools (ADR-019).

    Single source of truth: iterates ``_ARGS_BY_TOOL`` (deterministic order). Each entry carries
    the dotted domain ``name`` (NOT the anthropic-underscore transport name), description,
    ``mutating`` (name in MUTATING_TOOLS), ``execution`` ("server" for SERVER_SIDE_TOOLS else
    "client") and ``inputSchema`` (the args JSON Schema).
    """
    catalog: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        catalog.append(
            {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "mutating": name in MUTATING_TOOLS,
                "execution": "server" if name in SERVER_SIDE_TOOLS else "client",
                "inputSchema": tool_input_schema(name),
            }
        )
    return catalog


def anthropic_tool_definitions() -> list[dict[str, Any]]:
    """Tool definitions for the Anthropic messages API (input_schema per tool)."""
    definitions: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        definitions.append(
            {
                # BUG-3 forward map: Anthropic requires underscore names; iOS-facing names stay
                # dotted. `name` here is the domain name; emit the anthropic-name transport-side.
                "name": to_anthropic_tool_name(name),
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": tool_input_schema(name),
            }
        )
    return definitions
