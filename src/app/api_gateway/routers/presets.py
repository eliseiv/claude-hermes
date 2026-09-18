"""Presets catalog route: GET /v1/presets (ADR-035, localized ADR-069)."""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.presets import (
    DEFAULT_PRESET_LOCALE,
    canonicalize_preset_locale,
    preset_catalog,
)
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError, ValidationFailedError
from app.schemas.presets import PresetsResponse

router = APIRouter(prefix="/v1/presets", tags=["Presets"])


def resolve_presets_locale(
    query_locale: str | None,
    accept_language: str | None,
    default_locale: str,
) -> str:
    """Resolve the catalog locale by priority. Pure — no I/O.

    Order: explicit ``?locale=`` (invalid → 422) → ``Accept-Language`` (lenient) →
    per-instance default → ``en``.
    """
    if query_locale is not None:
        resolved = canonicalize_preset_locale(query_locale)
        if resolved is not None:
            return resolved
        raise ValidationFailedError(f"locale '{query_locale}' is not supported")

    header_locale = _first_supported_language(accept_language)
    if header_locale is not None:
        return header_locale

    default_resolved = canonicalize_preset_locale(default_locale)
    if default_resolved is not None:
        return default_resolved
    return DEFAULT_PRESET_LOCALE


def _first_supported_language(accept_language: str | None) -> str | None:
    """First supported locale from an ``Accept-Language`` header, else ``None`` (lenient)."""
    if not accept_language:
        return None
    for part in accept_language.split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        resolved = canonicalize_preset_locale(tag)
        if resolved is not None:
            return resolved
    return None


@router.get(
    "",
    response_model=PresetsResponse,
    summary="Каталог пресетов промтов",
    description=(
        "Возвращает список пресетов для чипов на главном экране чата: `id` (стабильный slug), "
        "`title`, `icon` (имя SF Symbol) и `prompt` (текст для подстановки в композер). Порядок "
        "элементов = порядок чипов на экране. Тексты `title` и `prompt` отдаются на выбранном "
        "языке: приоритет у параметра `locale`, затем заголовок `Accept-Language`, затем язык "
        "по умолчанию для инстанса; при отсутствии перевода используется английский. Поле "
        "`locale` в ответе сообщает фактически применённый язык. Read-only, без состояния."
    ),
)
async def list_presets(
    request: Request,
    current: CurrentUser,
    locale: str | None = Query(
        default=None,
        description=(
            "Желаемый язык каталога (`en` или `ru`). Если не указан — язык определяется по "
            "заголовку `Accept-Language`, иначе используется язык по умолчанию для инстанса. "
            "Недопустимое значение возвращает ошибку 422."
        ),
        examples=["ru"],
    ),
    accept_language: str | None = Header(default=None),
) -> PresetsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    resolved = resolve_presets_locale(
        query_locale=locale,
        accept_language=accept_language,
        default_locale=get_settings().resolved_presets_default_locale(),
    )
    return PresetsResponse.model_validate({"locale": resolved, "presets": preset_catalog(resolved)})
