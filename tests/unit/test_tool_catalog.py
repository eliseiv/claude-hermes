"""Unit tests for the GET /v1/tools catalog payload (ADR-019, chat-orchestrator/02).

``tool_catalog()`` is the single source of truth backing the endpoint; these tests assert the
catalog contract (13 tools, dotted domain names, correct mutating/execution flags, inputSchema)
without an app/DB round-trip. The HTTP wiring (JWT-protection, response shape) is exercised in
tests/integration/test_tools_endpoint.py.
"""

from __future__ import annotations

from app.chat.tools import (
    ALL_TOOL_NAMES,
    MUTATING_TOOLS,
    SERVER_SIDE_TOOLS,
    tool_catalog,
)

# Per ADR-011 / chat-orchestrator/02: 8 client-side iOS tools + 5 server-side site.* tools.
_EXPECTED_NAMES = {
    "files.read",
    "files.write",
    "files.list",
    "files.mkdir",
    "calendar.read",
    "calendar.create_events",
    "reminders.read",
    "reminders.create",
    "site.write_file",
    "site.preview",
    "site.list",
    "site.read",
    "site.delete",
}


def test_catalog_has_thirteen_tools() -> None:
    catalog = tool_catalog()
    assert len(catalog) == 13
    assert {t["name"] for t in catalog} == _EXPECTED_NAMES == set(ALL_TOOL_NAMES)


def test_every_tool_name_is_dotted_domain_not_underscore() -> None:
    # The iOS-facing contract uses dotted domain names (files.read, site.write_file); the
    # underscore wire names are an Anthropic-transport detail and must NOT leak here (BUG-3).
    for tool in tool_catalog():
        assert "." in tool["name"], tool["name"]
        assert "_" not in tool["name"].split(".")[0]  # the domain segment has no underscore


def test_mutating_flag_matches_mutating_tools() -> None:
    expected_mutating = {
        "files.write",
        "files.mkdir",
        "calendar.create_events",
        "reminders.create",
        "site.write_file",
        "site.delete",
    }
    assert expected_mutating == set(MUTATING_TOOLS)
    by_name = {t["name"]: t for t in tool_catalog()}
    for name, tool in by_name.items():
        assert isinstance(tool["mutating"], bool)
        assert tool["mutating"] is (name in expected_mutating), name


def test_execution_is_server_for_site_and_client_otherwise() -> None:
    by_name = {t["name"]: t for t in tool_catalog()}
    for name, tool in by_name.items():
        expected = "server" if name in SERVER_SIDE_TOOLS else "client"
        assert tool["execution"] == expected, (name, tool["execution"])
        # Cross-check: exactly the site.* tools are server-side.
        if name.startswith("site."):
            assert tool["execution"] == "server"
        else:
            assert tool["execution"] == "client"


def test_every_tool_has_input_schema_and_description() -> None:
    for tool in tool_catalog():
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"], f"{tool['name']} has empty inputSchema"
        # JSON Schema object shape (Pydantic emits type=object with properties for arg models).
        assert tool["inputSchema"].get("type") == "object"
        assert isinstance(tool["description"], str) and tool["description"]
