"""Unit tests for ADR-022: optional projectId + axis-A site.* gating by project presence.

Pure, no I/O. Two concerns:
- ChatRunRequest.projectId validator: optional (None ok), but a present-yet-blank value is 422.
- anthropic_tool_definitions(include_server_side=...) drops SERVER_SIDE_TOOLS (site.*) when False
  while keeping every client-side tool (files.*/calendar.*/reminders.*), and the full 13-tool set
  (incl. site.*) when True.

Axis B (assistant_mode) is intentionally NOT exercised: per the task and tools.py docstring it is
Q-012-1 Open and NOT implemented — the only code-level gate today is project_id (axis A).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.chat.tools import (
    ALL_TOOL_NAMES,
    SERVER_SIDE_TOOLS,
    anthropic_tool_definitions,
    to_anthropic_tool_name,
    to_domain_tool_name,
)
from app.schemas.chat import ChatRunRequest

_UID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _run_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"userId": str(_UID), "message": "hi", "mode": "credits"}
    base.update(overrides)
    return base


# ----------------------------- projectId validator (scenario 1) -----------------------------
def test_run_request_without_project_id_is_valid_and_none() -> None:
    req = ChatRunRequest.model_validate(_run_payload())
    assert req.projectId is None


def test_run_request_with_project_id_is_valid() -> None:
    req = ChatRunRequest.model_validate(_run_payload(projectId="proj-1"))
    assert req.projectId == "proj-1"


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t \n "])
def test_run_request_blank_project_id_rejected(blank: str) -> None:
    # ADR-022 §1: present-but-blank projectId is a 422 (not silently coerced to NULL).
    with pytest.raises(ValidationError, match="non-empty"):
        ChatRunRequest.model_validate(_run_payload(projectId=blank))


# ----------------------------- axis-A tool gating (scenario 2) -----------------------------
def _domain_names(*, include_server_side: bool) -> set[str]:
    defs = anthropic_tool_definitions(include_server_side=include_server_side)
    # definitions carry the anthropic wire (underscore) names — reverse-map to domain for asserts.
    return {to_domain_tool_name(d["name"]) for d in defs}


def test_definitions_with_project_include_full_set_of_14() -> None:
    names = _domain_names(include_server_side=True)
    assert names == set(ALL_TOOL_NAMES)
    assert len(names) == 14  # ADR-026: +time.now
    # site.* present.
    assert names >= SERVER_SIDE_TOOLS


def test_definitions_without_project_exclude_site_tools() -> None:
    names = _domain_names(include_server_side=False)
    # No site.* at all.
    assert names.isdisjoint(SERVER_SIDE_TOOLS)
    # Exactly the complement of project-scoped site.* (ADR-026: time.now stays — global, not gated).
    assert names == set(ALL_TOOL_NAMES) - set(SERVER_SIDE_TOOLS)
    assert len(names) == 9  # 8 client-side + time.now (14 - 5 site.*)
    assert "time.now" in names


def test_definitions_without_project_keep_all_client_side_tools() -> None:
    names = _domain_names(include_server_side=False)
    # ADR-022 §2: client-side tools are NOT touched by the project gate.
    for client_tool in ("files.read", "files.write", "files.list", "files.mkdir"):
        assert client_tool in names
    for client_tool in ("calendar.read", "calendar.create_events"):
        assert client_tool in names
    for client_tool in ("reminders.read", "reminders.create"):
        assert client_tool in names


def test_default_include_server_side_is_true() -> None:
    # Backwards-compatible default: omitting the flag keeps the full set (pre-ADR-022 behavior).
    assert {to_domain_tool_name(d["name"]) for d in anthropic_tool_definitions()} == set(
        ALL_TOOL_NAMES
    )


def test_emitted_names_are_wire_underscore_form() -> None:
    # Whichever gate, emitted names are always the underscore wire form (BUG-3), no dots.
    for flag in (True, False):
        defs = anthropic_tool_definitions(include_server_side=flag)
        names = {d["name"] for d in defs}
        assert all("." not in n for n in names)
        # Each emitted name reverse-maps to a known domain tool (bijective on the offered subset).
        assert names == {to_anthropic_tool_name(to_domain_tool_name(n)) for n in names}
