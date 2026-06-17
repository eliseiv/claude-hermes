"""Presets catalog route: GET /v1/presets (chat-orchestrator/02, ADR-035).

JWT-protected like GET /v1/tools and GET /v1/models (CurrentUser) — the list is not secret but
the /v1/* auth contour is uniform. Returns the static prompt-preset registry sourced from
``app.chat.presets`` (single source of truth). Read-only, no state/DB/ledger; per-user rate limit
as other reads. Provider/instance-agnostic — identical on every instance (ADR-033).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.presets import preset_catalog
from app.deps import CurrentUser
from app.errors import RateLimitedError
from app.schemas.presets import PresetsResponse

router = APIRouter(prefix="/v1/presets", tags=["Presets"])


@router.get(
    "",
    response_model=PresetsResponse,
    summary="Каталог пресетов промтов",
    description=(
        "Возвращает список пресетов для чипов на главном экране чата: `id` (стабильный slug), "
        "`title`, `icon` (имя SF Symbol) и `prompt` (текст для подстановки в композер). Порядок "
        "элементов = порядок чипов на экране. Read-only, без состояния."
    ),
)
async def list_presets(request: Request, current: CurrentUser) -> PresetsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    return PresetsResponse.model_validate({"presets": preset_catalog()})
