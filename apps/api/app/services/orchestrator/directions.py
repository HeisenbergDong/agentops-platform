import re

DEFAULT_DAILY_TARGET = 100
MAX_ROUNDS_PER_DIRECTION = 5

AUTO_SCOPE_MARKERS = {"不限", "不限定", "随机", "随便", "auto", "any"}


def normalize_job_directions(items: list[str], daily_target: int = DEFAULT_DAILY_TARGET) -> list[str]:
    split_items: list[str] = []
    for item in items or []:
        split_items.extend(split_direction_text(item))
    unique_items = _dedupe_preserving_order(split_items)
    return expand_directions_for_target(unique_items, daily_target=daily_target)


def split_direction_text(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.lower() in AUTO_SCOPE_MARKERS:
        return []
    parts = [
        _strip_direction_prefix(line)
        for line in re.split(r"[\r\n]+", text)
        if _strip_direction_prefix(line)
    ]
    if len(parts) <= 1:
        parts = [
            _strip_direction_prefix(part)
            for part in re.split(r"(?:^|\s)(?:\d+|[一二三四五六七八九十]+)[\.、)]\s*", text)
            if _strip_direction_prefix(part)
        ]
    if len(parts) <= 1:
        parts = [
            _strip_direction_prefix(part)
            for part in re.split(r"[；;]", text)
            if _strip_direction_prefix(part)
        ]
    return parts


def expand_directions_for_target(
    directions: list[str],
    daily_target: int = DEFAULT_DAILY_TARGET,
    max_rounds_per_direction: int = MAX_ROUNDS_PER_DIRECTION,
) -> list[str]:
    clean = _dedupe_preserving_order(directions)
    if not clean:
        return []
    required_projects = max(1, (max(1, int(daily_target)) + max_rounds_per_direction - 1) // max_rounds_per_direction)
    if len(clean) >= required_projects:
        return clean
    expanded = list(clean)
    variant_index = 1
    while len(expanded) < required_projects:
        for direction in clean:
            if len(expanded) >= required_projects:
                break
            expanded.append(f"{direction}（扩展项目 {variant_index}）")
        variant_index += 1
    return expanded


def _strip_direction_prefix(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*•]\s*", "", text)
    text = re.sub(r"^\s*(?:\d+|[一二三四五六七八九十]+)[\.、)]\s*", "", text)
    return text.strip(" \t，,；;。")


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", "", text).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
