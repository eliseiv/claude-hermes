"""Unit tests for the prompt-presets registry (ADR-035): app.chat.presets.preset_catalog().

The catalog is pure (no I/O, no state, no DB) and provider/instance-agnostic. It returns the
seven static presets in declaration order (= chip order on the chat home screen) as a list of
``{id, title, icon, prompt}`` dicts. Each field is required and non-empty; ids are unique
snake_case slugs.
"""

from __future__ import annotations

import re

from app.chat.presets import preset_catalog

# Canonical order and id set (ADR-035 §2) — declaration order IS the chip order.
_EXPECTED_IDS = [
    "plan_week",
    "meeting_notes",
    "tasks_from_photo",
    "design_brief",
    "daily_review",
    "summarize_text",
    "project_structure",
]
_SNAKE_CASE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FIELDS = ("id", "title", "icon", "prompt")


def test_preset_catalog_has_seven_entries() -> None:
    assert len(preset_catalog()) == 7


def test_preset_catalog_deterministic_order() -> None:
    # Pure & deterministic: declaration order, stable across calls.
    ids = [p["id"] for p in preset_catalog()]
    assert ids == _EXPECTED_IDS
    # Calling twice yields the identical structure (no hidden state / shuffling).
    assert preset_catalog() == preset_catalog()


def test_preset_catalog_is_pure_no_shared_mutable_state() -> None:
    # Mutating a returned copy must not leak into the next call's result.
    first = preset_catalog()
    first[0]["title"] = "MUTATED"
    first.append({"id": "x", "title": "x", "icon": "x", "prompt": "x"})
    second = preset_catalog()
    assert len(second) == 7
    assert second[0]["title"] != "MUTATED"


def test_preset_catalog_all_four_fields_present_and_non_empty() -> None:
    for p in preset_catalog():
        assert set(p.keys()) == set(_FIELDS), f"unexpected fields on preset {p}"
        for field in _FIELDS:
            value = p[field]
            assert isinstance(value, str), f"{field} not a str on {p['id']}"
            assert value.strip(), f"{field} empty on preset {p['id']}"


def test_preset_catalog_ids_unique_snake_case() -> None:
    ids = [p["id"] for p in preset_catalog()]
    assert len(ids) == len(set(ids)), f"duplicate preset ids: {ids}"
    for pid in ids:
        assert _SNAKE_CASE.match(pid), f"id is not snake_case [a-z0-9_]: {pid!r}"
