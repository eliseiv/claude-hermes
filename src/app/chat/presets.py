"""Prompt presets registry (ADR-035): static catalog for GET /v1/presets.

Single source of truth for the chat home-screen preset chips (Plan Week, Meeting Notes, …).
By the same pattern as ``tool_catalog()`` (``app.chat.tools``): a module-level static list +
a pure ``preset_catalog()`` that returns the entries in declaration order (= chip order on
screen). No I/O, no state, no DB; provider/instance-agnostic — identical on every instance
(ADR-033). Editing presets without a deploy (config-JSON / DB) is deferred — TD-026.

Each preset carries:
- ``id``    — stable snake_case slug (``[a-z0-9_]``), unique in the set; stable across releases.
- ``title`` — chip display name (as on the design, e.g. "Plan Week").
- ``icon``  — SF Symbol name (ADR-035 §4); the iOS client renders it via ``Image(systemName:)``.
- ``prompt``— plain text inserted into the composer on tap (EN; no i18n on start — Q-035-2).
"""

from __future__ import annotations

from typing import Any, NamedTuple


class Preset(NamedTuple):
    """One prompt preset (ADR-035 §1). All four fields are required and non-empty."""

    id: str
    title: str
    icon: str
    prompt: str


# Static registry — single source of truth (ADR-035 §2/§3). Declaration order IS the chip order
# on the chat home screen. Editing without a deploy is intentionally out of scope (TD-026).
_PRESETS: tuple[Preset, ...] = (
    Preset(
        id="plan_week",
        title="Plan Week",
        icon="calendar",
        prompt=(
            "Help me plan my upcoming week. Ask me about my priorities, deadlines, and "
            "commitments, then propose a balanced day-by-day schedule."
        ),
    ),
    Preset(
        id="meeting_notes",
        title="Meeting Notes",
        icon="person.2",
        prompt=(
            "Turn my raw meeting notes into a clean summary with key decisions, action items "
            "(with owners), and open questions. I'll paste the notes next."
        ),
    ),
    Preset(
        id="tasks_from_photo",
        title="Tasks from Photo",
        icon="camera",
        prompt=(
            "I'll attach a photo of a note, whiteboard, or list. Extract every actionable task "
            "from it and return them as a clear checklist."
        ),
    ),
    Preset(
        id="design_brief",
        title="Design Brief",
        icon="paintbrush",
        prompt=(
            "Help me write a concise design brief. Ask me about the goal, audience, scope, "
            "constraints, and success criteria, then draft the brief."
        ),
    ),
    Preset(
        id="daily_review",
        title="Daily Review",
        icon="checklist",
        prompt=(
            "Guide me through a short daily review: what I accomplished, what's still open, and "
            "the top 3 priorities for tomorrow."
        ),
    ),
    Preset(
        id="summarize_text",
        title="Summarize Text",
        icon="doc.text",
        prompt=(
            "Summarize the text I provide. Give a 3-sentence overview, then key points as "
            "bullets. I'll paste the text next."
        ),
    ),
    Preset(
        id="project_structure",
        title="Project Structure",
        icon="folder",
        prompt=(
            "Help me design a project structure. Ask about the project type and goals, then "
            "propose a folder/file layout with a short rationale."
        ),
    ),
)


def preset_catalog() -> list[dict[str, Any]]:
    """Machine-readable catalog of prompt presets for GET /v1/presets (ADR-035).

    Pure (no I/O): iterates the static ``_PRESETS`` registry in declaration order (= chip order)
    and returns a list of ``{id, title, icon, prompt}`` dicts. Identical on every instance.
    """
    return [{"id": p.id, "title": p.title, "icon": p.icon, "prompt": p.prompt} for p in _PRESETS]
