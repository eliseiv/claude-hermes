"""Prompt presets registry (ADR-035, localized by ADR-069): catalog for GET /v1/presets.

``id`` and ``icon`` are stable machine identifiers and are NOT localized; ``title`` and
``prompt`` carry one string per locale. EN is the canon and per-field fallback.
"""

from __future__ import annotations

from typing import Any, NamedTuple

SUPPORTED_PRESET_LOCALES: tuple[str, ...] = ("en", "ru")
DEFAULT_PRESET_LOCALE: str = "en"


def canonicalize_preset_locale(raw: str | None) -> str | None:
    """Map a client locale tag onto ``SUPPORTED_PRESET_LOCALES``, or ``None`` if unsupported.

    Matching is case-insensitive and accepts ``_`` as ``-``. Tries the full tag, then
    progressively shorter prefixes (``ru-RU`` → ``ru``). Empty / blank → ``None``.
    """
    if raw is None:
        return None
    tag = raw.strip().lower().replace("_", "-")
    if not tag:
        return None
    by_lower = {locale.lower(): locale for locale in SUPPORTED_PRESET_LOCALES}
    if tag in by_lower:
        return by_lower[tag]
    parts = tag.split("-")
    for length in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[:length])
        if candidate in by_lower:
            return by_lower[candidate]
    return None


class Preset(NamedTuple):
    """One prompt preset. ``id``/``icon`` are locale-independent; ``title``/``prompt`` are maps."""

    id: str
    icon: str
    title: dict[str, str]
    prompt: dict[str, str]


_PRESETS: tuple[Preset, ...] = (
    Preset(
        id="plan_week",
        icon="calendar",
        title={"en": "Plan Week", "ru": "Планирование недели"},
        prompt={
            "en": (
                "Help me plan my upcoming week. Ask me about my priorities, deadlines, and "
                "commitments, then propose a balanced day-by-day schedule."
            ),
            "ru": (
                "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и "
                "обязательствах, а затем предложи сбалансированное расписание по дням."
            ),
        },
    ),
    Preset(
        id="meeting_notes",
        icon="person.2",
        title={"en": "Meeting Notes", "ru": "Заметки со встречи"},
        prompt={
            "en": (
                "Turn my raw meeting notes into a clean summary with key decisions, action items "
                "(with owners), and open questions. I'll paste the notes next."
            ),
            "ru": (
                "Преврати мои черновые заметки со встречи в аккуратное резюме: ключевые решения, "
                "задачи с ответственными и открытые вопросы. Я вставлю заметки следующим "
                "сообщением."
            ),
        },
    ),
    Preset(
        id="tasks_from_photo",
        icon="camera",
        title={"en": "Tasks from Photo", "ru": "Задачи с фото"},
        prompt={
            "en": (
                "I'll attach a photo of a note, whiteboard, or list. Extract every actionable task "
                "from it and return them as a clear checklist."
            ),
            "ru": (
                "Я прикреплю фото заметки, доски или списка. Выдели из него все конкретные "
                "задачи и верни их в виде понятного чек-листа."
            ),
        },
    ),
    Preset(
        id="design_brief",
        icon="paintbrush",
        title={"en": "Design Brief", "ru": "Дизайн-бриф"},
        prompt={
            "en": (
                "Help me write a concise design brief. Ask me about the goal, audience, scope, "
                "constraints, and success criteria, then draft the brief."
            ),
            "ru": (
                "Помоги составить лаконичный дизайн-бриф. Расспроси меня о цели, аудитории, объёме "
                "работ, ограничениях и критериях успеха, а затем подготовь бриф."
            ),
        },
    ),
    Preset(
        id="daily_review",
        icon="checklist",
        title={"en": "Daily Review", "ru": "Итоги дня"},
        prompt={
            "en": (
                "Guide me through a short daily review: what I accomplished, what's still open, "
                "and the top 3 priorities for tomorrow."
            ),
            "ru": (
                "Проведи меня через короткий разбор дня: что удалось сделать, что осталось "
                "незавершённым и какие три главных приоритета на завтра."
            ),
        },
    ),
    Preset(
        id="summarize_text",
        icon="doc.text",
        title={"en": "Summarize Text", "ru": "Краткое изложение"},
        prompt={
            "en": (
                "Summarize the text I provide. Give a 3-sentence overview, then key points as "
                "bullets. I'll paste the text next."
            ),
            "ru": (
                "Кратко изложи текст, который я пришлю. Дай обзор в трёх предложениях, а затем "
                "ключевые мысли списком. Я вставлю текст следующим сообщением."
            ),
        },
    ),
    Preset(
        id="project_structure",
        icon="folder",
        title={"en": "Project Structure", "ru": "Структура проекта"},
        prompt={
            "en": (
                "Help me design a project structure. Ask about the project type and goals, then "
                "propose a folder/file layout with a short rationale."
            ),
            "ru": (
                "Помоги продумать структуру проекта. Расспроси о типе проекта и целях, а затем "
                "предложи структуру папок и файлов с кратким обоснованием."
            ),
        },
    ),
)


def preset_catalog(locale: str = DEFAULT_PRESET_LOCALE) -> list[dict[str, Any]]:
    """Catalog of prompt presets for the given locale. Per-field EN fallback. Pure."""
    return [
        {
            "id": p.id,
            "title": p.title.get(locale) or p.title[DEFAULT_PRESET_LOCALE],
            "icon": p.icon,
            "prompt": p.prompt.get(locale) or p.prompt[DEFAULT_PRESET_LOCALE],
        }
        for p in _PRESETS
    ]
