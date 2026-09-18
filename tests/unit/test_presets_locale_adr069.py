"""Unit: locale resolution for GET /v1/presets (ADR-069)."""

from __future__ import annotations

import pytest

from app.api_gateway.routers.presets import resolve_presets_locale
from app.chat.presets import canonicalize_preset_locale, preset_catalog
from app.errors import ValidationFailedError

_DEFAULT = "en"


def test_query_locale_ru_selected() -> None:
    assert resolve_presets_locale("ru", None, _DEFAULT) == "ru"


def test_query_locale_uppercase_normalized() -> None:
    assert resolve_presets_locale("RU", None, _DEFAULT) == "ru"


def test_query_locale_beats_accept_language() -> None:
    assert resolve_presets_locale("en", "ru-RU", _DEFAULT) == "en"


def test_query_locale_unsupported_raises_422() -> None:
    with pytest.raises(ValidationFailedError) as exc:
        resolve_presets_locale("de", "ru-RU", _DEFAULT)
    assert exc.value.status_code == 422
    assert "de" in str(exc.value)


def test_accept_language_ru_ru() -> None:
    assert resolve_presets_locale(None, "ru-RU,en;q=0.8", _DEFAULT) == "ru"


def test_accept_language_unsupported_falls_to_default() -> None:
    assert resolve_presets_locale(None, "de-DE,fr;q=0.8", "ru") == "ru"


def test_canonicalize_prefix() -> None:
    assert canonicalize_preset_locale("ru-RU") == "ru"
    assert canonicalize_preset_locale("en_US") == "en"
    assert canonicalize_preset_locale("zh") is None


def test_ru_catalog_titles() -> None:
    titles = {p["id"]: p["title"] for p in preset_catalog("ru")}
    assert titles["plan_week"] == "Планирование недели"
    assert titles["meeting_notes"] == "Заметки со встречи"


def test_unknown_locale_falls_to_en() -> None:
    en = preset_catalog("en")
    mystery = preset_catalog("xx")
    assert [p["title"] for p in en] == [p["title"] for p in mystery]
