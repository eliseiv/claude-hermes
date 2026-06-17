"""Presets-catalog schema for GET /v1/presets (chat-orchestrator/02, ADR-035).

Provider-agnostic, read-only contract: the static prompt-preset registry as a list of
``{id, title, icon, prompt}`` items. No state, no DB; identical on every instance (ADR-033).
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel


class PresetInfo(StrictModel):
    id: str = Field(
        description=(
            "Стабильный slug пресета (snake_case, `[a-z0-9_]`), уникален в наборе. Стабилен "
            "между релизами; пригоден для аналитики/кэша на клиенте."
        )
    )
    title: str = Field(description="Отображаемое имя чипа (например `Plan Week`).")
    icon: str = Field(
        description=(
            "Имя SF Symbol (например `calendar`); рисуется на iOS через `Image(systemName:)`."
        )
    )
    prompt: str = Field(
        description="Текст промта, подставляемый в композер при тапе по чипу (plain text)."
    )


class PresetsResponse(StrictModel):
    presets: list[PresetInfo] = Field(
        description=(
            "Каталог пресетов промтов для чипов на главном экране чата (порядок = порядок чипов)."
        )
    )
