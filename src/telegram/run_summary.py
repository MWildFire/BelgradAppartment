"""One summary format for bot and userbot; unresolved work is never called completed."""

from __future__ import annotations


def format_run_summary(
    *,
    matched: int,
    no_match: int,
    errors: int = 0,
    needs_review: int = 0,
    pending: int = 0,
    unresolved: int = 0,
    selected: int | None = None,
    duration_seconds: float | None = None,
    label: str = "Прогон обработки",
) -> str:
    remaining = needs_review + pending + unresolved
    state = "требует внимания" if errors or remaining else "завершён"
    lines = [f"📊 {label} — {state}", ""]
    if selected is not None:
        lines.append(f"Выбрано автоматически: {selected}")
    lines.extend(
        [
            f"Обработано: {matched + no_match}",
            f"✅ Найдено матчей: {matched}",
            f"❌ Без совпадения: {no_match}",
        ]
    )
    if selected is not None or remaining:
        lines.extend(
            [
                f"👤 Требуют решения человека: {needs_review}",
                f"⏳ Отложены или недоступны автоматике: {pending}",
            ]
        )
    if unresolved:
        lines.append(f"❓ Прочие нерешённые: {unresolved}")
    if errors:
        lines.append(f"⚠️ Ошибки: {errors}")
    if duration_seconds is not None:
        if duration_seconds < 60:
            duration = f"{duration_seconds:.0f} сек"
        elif duration_seconds < 3600:
            duration = f"{int(duration_seconds // 60)} мин {int(duration_seconds % 60)} сек"
        else:
            hours, rem = divmod(int(duration_seconds), 3600)
            duration = f"{hours} ч {rem // 60} мин"
        lines.append(f"⏱ Время: {duration}")
    return "\n".join(lines)
