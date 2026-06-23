import re

DEFAULT_DAILY_TARGET = 100
MAX_ROUNDS_PER_DIRECTION = 5

AUTO_SCOPE_MARKERS = {"不限", "不限定", "随机", "随便", "auto", "any"}
NUMBERED_HEADING_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\d+|[一二三四五六七八九十]+)[\.、)]\s*(?P<body>.+?)\s*$")


def normalize_job_directions(items: list[str], daily_target: int = DEFAULT_DAILY_TARGET) -> list[str]:
    split_items: list[str] = []
    for item in items or []:
        split_items.extend(split_direction_text(item))
    unique_items = _fold_flat_direction_items(_dedupe_preserving_order(split_items))
    return expand_directions_for_target(unique_items, daily_target=daily_target)


def split_direction_text(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.lower() in AUTO_SCOPE_MARKERS:
        return []
    numbered_sections = _split_numbered_sections(text)
    if numbered_sections:
        return numbered_sections
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
    return clean


def build_range_plan(directions: list[str], daily_target: int = DEFAULT_DAILY_TARGET) -> dict:
    clean = _dedupe_preserving_order(directions)
    if not clean:
        return {
            "total_target_rounds": max(1, int(daily_target or DEFAULT_DAILY_TARGET)),
            "ranges": [],
            "synthetic_range_policy": {},
        }
    total = max(1, int(daily_target or DEFAULT_DAILY_TARGET))
    explicit_total = min(total, MAX_ROUNDS_PER_DIRECTION * len(clean))
    min_rounds_per_direction = 2 if explicit_total >= len(clean) * 2 else 1
    weights = [_direction_complexity_weight(item) for item in clean]
    weight_total = sum(weights) or len(clean)
    raw_targets = [
        min(MAX_ROUNDS_PER_DIRECTION, max(min_rounds_per_direction, int(round(explicit_total * weight / weight_total))))
        for weight in weights
    ]
    diff = explicit_total - sum(raw_targets)
    index = 0
    while diff != 0 and raw_targets:
        offset = 1 if diff > 0 else -1
        target_index = index % len(raw_targets)
        next_value = raw_targets[target_index] + offset
        if min_rounds_per_direction <= next_value <= MAX_ROUNDS_PER_DIRECTION:
            raw_targets[target_index] = next_value
            diff -= offset
        index += 1
        if index > len(raw_targets) * MAX_ROUNDS_PER_DIRECTION * 2:
            break
    ranges = [
        {
            "range_id": f"range_{index + 1}",
            "title": _range_title(direction),
            "source_text": direction,
            "target_rounds": raw_targets[index],
            "completed_rounds": 0,
            "status": "active" if index == 0 else "pending",
            "project_policy": "独立项目，不与其他范围合并",
            "module_map": _module_map_for_direction(direction),
        }
        for index, direction in enumerate(clean)
    ]
    return {
        "total_target_rounds": total,
        "ranges": ranges,
        "synthetic_range_policy": {
            "allowed": True,
            "condition": "用户给定范围都完成但总轮次不足，或继续当前范围会重复、过小或硬编需求。",
            "must_be_related_to": [_range_title(item) for item in clean],
            "examples": _synthetic_range_examples(clean),
        },
    }


def _strip_direction_prefix(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*•]\s*", "", text)
    text = re.sub(r"^\s*(?:\d+|[一二三四五六七八九十]+)[\.、)]\s*", "", text)
    return text.strip(" \t，,；;。")


def _split_numbered_sections(text: str) -> list[str]:
    sections: list[tuple[str, list[str]]] = []
    global_constraints: list[str] = []
    current: tuple[str, list[str]] | None = None
    for raw_line in re.split(r"[\r\n]+", text):
        line = str(raw_line or "").strip()
        if not line:
            continue
        match = NUMBERED_HEADING_RE.match(line)
        if match:
            title = _strip_direction_prefix(match.group("body"))
            if title:
                current = (title, [])
                sections.append(current)
            continue
        if current is None:
            if constraint := _global_constraint_from_line(line):
                global_constraints.append(constraint)
            continue
        if _looks_like_global_note(line):
            if constraint := _global_constraint_from_line(line):
                global_constraints.append(constraint)
            continue
        detail = _strip_direction_prefix(line)
        if detail:
            current[1].append(detail)
    if not sections:
        return []
    constraints = _dedupe_preserving_order(global_constraints)
    titles = [title for title, _details in sections]
    return [
        _format_numbered_section(title, details, _constraints_for_section(title, constraints, titles))
        for title, details in sections
    ]


def _format_numbered_section(title: str, details: list[str], constraints: list[str]) -> str:
    title = _strip_direction_prefix(title)
    details = [_strip_direction_prefix(item) for item in details if _strip_direction_prefix(item)]
    if details and not title.endswith(("：", ":")):
        body = f"{title}：{'；'.join(details)}"
    elif details:
        body = f"{title}{'；'.join(details)}"
    else:
        body = title
    if constraints:
        body = f"{body}。整体约束：{'；'.join(constraints)}"
    return body.strip(" \t，,；;。")


def _looks_like_global_note(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    if _global_constraint_from_line(text):
        return True
    return any(marker in text for marker in ("以上", "这些范围", "两个范围", "几个范围", "范围入手", "从以上", "就从"))


def _global_constraint_from_line(line: str) -> str:
    text = _strip_direction_prefix(line)
    if not text:
        return ""
    constraint_markers = (
        "前后端分离",
        "技术栈",
        "技术选型",
        "优先用",
        "优先使用",
        "不要做营销页",
        "不要营销页",
        "首屏",
        "导航",
    )
    if any(marker in text for marker in constraint_markers):
        return text
    return ""


def _constraints_for_section(title: str, constraints: list[str], titles: list[str]) -> list[str]:
    return [item for item in constraints if not _constraint_mentions_other_section(item, title, titles)]


def _constraint_mentions_other_section(constraint: str, current_title: str, titles: list[str]) -> bool:
    current_labels = set(_section_label_variants(current_title))
    for title in titles:
        if title == current_title:
            continue
        for label in _section_label_variants(title):
            if not label or label in current_labels:
                continue
            if label in constraint:
                return True
    return False


def _section_label_variants(title: str) -> list[str]:
    head = re.split(r"[：:，,。；;\s]", str(title or "").strip(), maxsplit=1)[0].strip()
    labels = [head]
    for suffix in ("服务平台", "管理平台", "业务平台", "平台", "管理系统", "系统", "服务"):
        if head.endswith(suffix) and len(head) > len(suffix) + 1:
            labels.append(head[: -len(suffix)])
    result: list[str] = []
    seen: set[str] = set()
    for label in labels:
        clean = re.sub(r"\s+", "", label)
        if len(clean) < 2 or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _fold_flat_direction_items(items: list[str]) -> list[str]:
    clean = _dedupe_preserving_order(items)
    if len(clean) <= 2:
        return clean
    heading_indexes = [index for index, item in enumerate(clean) if _looks_like_flat_top_level_direction(item)]
    if len(heading_indexes) < 2:
        return [item for item in clean if not _looks_like_flat_global_note(item)]

    constraints = [_strip_direction_prefix(item) for item in clean if _looks_like_flat_global_constraint(item)]
    folded: list[str] = []
    for pos, start in enumerate(heading_indexes):
        end = heading_indexes[pos + 1] if pos + 1 < len(heading_indexes) else len(clean)
        title = _strip_direction_prefix(clean[start])
        details: list[str] = []
        for item in clean[start + 1 : end]:
            text = _strip_direction_prefix(item)
            if not text or _looks_like_flat_global_note(text):
                continue
            details.append(text)
        folded.append(_format_numbered_section(title, details, constraints))
    return _dedupe_preserving_order(folded)


def _looks_like_flat_top_level_direction(item: str) -> bool:
    text = _strip_direction_prefix(item)
    if not text or _looks_like_flat_global_note(text):
        return False
    head = re.split(r"[\uff1a:;；。，\s]", text, maxsplit=1)[0].strip()
    if not head or len(head) > 24:
        return False
    if head.endswith(("\u7b49", "\u7b49\u7b49", "\u7b49\u3002")):
        return False
    lower = head.lower()
    if lower.startswith(("can ", "able ", "should ")):
        return False
    action_prefixes = (
        "\u53ef\u4ee5",
        "\u80fd\u591f",
        "\u7528\u6237",
        "\u8bf4\u660e",
        "\u627e",
        "\u652f\u4ed8",
        "\u667a\u80fd",
        "\u6743\u9650",
        "\u540e\u7eed",
        "\u5c3d\u91cf",
        "\u5c31\u4ece",
    )
    if any(head.startswith(prefix) for prefix in action_prefixes):
        return False
    platform_markers = (
        "\u5e73\u53f0",
        "\u7cfb\u7edf",
        "\u5e94\u7528",
        "\u5de5\u4f5c\u53f0",
        "\u540e\u53f0",
        "platform",
        "system",
        "app",
    )
    return any(marker in lower or marker in head for marker in platform_markers)


def _looks_like_flat_global_note(item: str) -> bool:
    text = _strip_direction_prefix(item)
    if not text:
        return False
    if _looks_like_flat_global_constraint(text):
        return True
    note_markers = (
        "\u4ee5\u4e0a",
        "\u4e24\u4e2a\u8303\u56f4",
        "\u51e0\u4e2a\u8303\u56f4",
        "\u8303\u56f4\u5165\u624b",
        "\u5c31\u4ece",
    )
    return any(marker in text for marker in note_markers)


def _looks_like_flat_global_constraint(item: str) -> bool:
    text = _strip_direction_prefix(item)
    constraint_markers = (
        "\u524d\u540e\u7aef\u5206\u79bb",
        "\u6280\u672f\u6808",
        "\u4e0d\u8981\u505a\u8425\u9500\u9875",
        "frontend",
        "backend",
    )
    return any(marker.lower() in text.lower() for marker in constraint_markers)


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


def _range_title(direction: str) -> str:
    text = str(direction or "").strip()
    head = re.split(r"[：:，,。；;\s]", text, maxsplit=1)[0].strip()
    return head or text[:30] or "业务系统"


def _direction_complexity_weight(direction: str) -> int:
    text = str(direction or "")
    weight = 10
    weight += min(8, text.count("；") + text.count(";") + text.count("、") + text.count("，"))
    if any(marker in text for marker in ("平台", "系统", "工作台", "后台")):
        weight += 4
    if any(marker in text for marker in ("前后端分离", "角色", "权限", "支付", "订单", "认证", "统计", "流程")):
        weight += 4
    return weight


def _module_map_for_direction(direction: str) -> list[str]:
    text = str(direction or "")
    if any(marker in text for marker in ("招聘", "简历", "候选", "面试", "职位")):
        return [
            "系统骨架",
            "职位管理",
            "候选人管理",
            "简历筛选",
            "面试安排",
            "面试反馈",
            "Offer 流程",
            "部门用人需求",
            "招聘负责人工作台",
            "通知提醒",
            "统计看板",
            "角色权限",
            "异常状态",
            "运行构建",
        ]
    if any(marker in text for marker in ("中介", "撮合", "服务商", "匹配", "下单")):
        return [
            "系统骨架",
            "服务商入驻",
            "实名认证",
            "需求发布",
            "撮合推荐",
            "订单管理",
            "合同协议",
            "付款节点",
            "服务进度",
            "评价投诉",
            "平台介入",
            "结算统计",
            "角色权限",
            "异常处理",
            "运行构建",
        ]
    return [
        "系统骨架",
        "主列表",
        "详情视图",
        "新增编辑",
        "状态流转",
        "搜索筛选",
        "统计联动",
        "角色权限",
        "异常状态",
        "空状态",
        "运行构建",
    ]


def _synthetic_range_examples(directions: list[str]) -> list[str]:
    text = " ".join(directions)
    if any(marker in text for marker in ("招聘", "中介", "撮合", "服务商")):
        return ["招聘运营数据看板", "服务商管理后台", "撮合订单结算台", "投诉仲裁工作台"]
    if any(marker in text for marker in ("物流", "仓储", "库存", "配送")):
        return ["物流异常处置台", "仓配联动看板", "库存预警工作台", "运输成本核算台"]
    return ["运营管理后台", "业务审核工作台", "数据统计看板", "异常处理中心"]
