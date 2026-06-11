import json
import re
from pathlib import Path


IGNORED_DIRS = {"node_modules", "dist", "build", "target", ".venv", "__pycache__", ".git", ".npm-cache"}
SOURCE_EXTS = {".vue", ".js", ".ts", ".tsx", ".jsx", ".html", ".py", ".go", ".java"}
TEXT_EXTS = {*SOURCE_EXTS, ".css", ".md", ".json", ".yaml", ".yml"}


def review_project_static(root: Path, prompt: str = "", changed_files=None) -> dict:
    files = iter_project_files(root)
    combined, by_rel = read_text_files(root, files)
    changed_rel = normalize_changed_files(changed_files, root)
    focused_by_rel = changed_file_map(by_rel, changed_rel)
    issues: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []
    code_files = [path for path in files if path.suffix.lower() in SOURCE_EXTS | {".css"}]

    if not code_files:
        issues.append("项目目录里没有可审查的代码文件，无法形成可运行产物。")
    elif len(code_files) <= 2 and len(prompt) > 80:
        warnings.append(f"只发现 {len(code_files)} 个主要代码文件，当前需求要求的是多模块系统雏形，这个交付规模和我的需求不匹配。")

    lowered = combined.lower()
    keywords = prompt_keywords(prompt)
    hit_keywords = [word for word in keywords if word.lower() in lowered or word in combined]
    if not focused_by_rel and keywords and len(hit_keywords) < max(2, min(6, len(keywords) // 3)):
        warnings.append("源码里的业务实体、状态和操作命名没有覆盖当前项目方向，产物和我的需求不匹配。")

    for label, terms in expected_terms(prompt).items():
        if not any(term.lower() in lowered or term in combined for term in terms):
            issues.append(f"没有在源码里看到“{label}”相关实现痕迹。")

    if any(path.suffix.lower() in {".vue", ".js", ".ts", ".tsx", ".jsx", ".html"} for path in code_files):
        if not re.search(r"(@click|onclick|addEventListener|onClick|v-model|input|select|button)", combined):
            issues.append("前端源码里没有看到明显按钮、输入、选择或点击事件，交互性不足。")
        if re.search(r"(?<![-_\w])(TODO|FIXME)(?![-_\w])|todo\s*:", combined, re.IGNORECASE):
            warnings.append("源码仍包含 TODO 标记，存在未完成逻辑。")

    code_scope = focused_by_rel or by_rel
    code_issues, code_warnings = code_specific_findings(code_scope)
    issues.extend(code_issues)
    warnings.extend(code_warnings)
    evidence.append(f"审查了 {len(files)} 个项目文件，其中主要代码文件 {len(code_files)} 个。")
    if changed_rel:
        if focused_by_rel:
            evidence.append("本轮代码问题优先审查变更文件：" + "、".join(list(focused_by_rel.keys())[:8]))
        else:
            warnings.append("采集到了本轮变更文件，但没有在项目目录中匹配到对应源码，验收证据不完整。")
    if hit_keywords:
        evidence.append("源码命中的需求关键词：" + "、".join(hit_keywords[:10]))

    return {
        "ok": not issues,
        "issues": issues[:12],
        "warnings": warnings[:8],
        "evidence": evidence[:12],
        "stack": detect_stack(root, files),
        "file_count": len(files),
        "changed_files": sorted(changed_rel),
    }


def iter_project_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            relative_parts = path.parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            continue
        if path.suffix.lower() in TEXT_EXTS:
            files.append(path)
    return sorted(files, key=lambda item: (project_file_rank(item, root), str(item)))[:800]


def project_file_rank(path: Path, root: Path) -> tuple[int, int, int, str]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = rel.parts
    suffix = path.suffix.lower()
    source_rank = 0 if suffix in SOURCE_EXTS else 1
    depth_rank = len(parts)
    size_rank = min(path.stat().st_size if path.exists() else 0, 100000)
    return source_rank, depth_rank, size_rank, str(rel)


def read_text_files(root: Path, files: list[Path]) -> tuple[str, dict[str, str]]:
    chunks: list[str] = []
    by_rel: dict[str, str] = {}
    budget = 320000
    for path in files:
        if budget <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(text) > 30000:
            text = text[:15000] + "\n...<truncated>...\n" + text[-8000:]
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        by_rel[rel] = text
        chunk = f"\n--- {rel} ---\n{text}"
        chunks.append(chunk[:budget])
        budget -= len(chunk)
    return "\n".join(chunks), by_rel


def detect_stack(root: Path, files: list[Path]) -> list[str]:
    names = {path.name.lower() for path in files}
    suffixes = {path.suffix.lower() for path in files}
    stack: list[str] = []
    if "package.json" in names:
        package = load_package_json(root)
        deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
        if "vue" in deps or any(path.suffix.lower() == ".vue" for path in files):
            stack.append("vue")
        elif "react" in deps:
            stack.append("react")
        else:
            stack.append("node")
    if ".py" in suffixes or "requirements.txt" in names or "pyproject.toml" in names:
        stack.append("python")
    if ".go" in suffixes or "go.mod" in names:
        stack.append("go")
    if "pom.xml" in names or ".java" in suffixes:
        stack.append("java")
    if ".html" in suffixes and not stack:
        stack.append("static-web")
    return stack or ["unknown"]


def load_package_json(root: Path) -> dict:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def prompt_keywords(prompt: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", str(prompt or "")):
        if token in {"一个", "这个", "需求", "页面", "项目", "实现", "继续", "基于", "可以", "需要", "不要", "尽量", "简单"}:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:36]


def expected_terms(prompt: str) -> dict[str, list[str]]:
    text = str(prompt or "")
    groups: dict[str, list[str]] = {}
    if any(term in text for term in ["筛选", "过滤"]):
        groups["筛选"] = ["筛选", "filter", "active", "selected"]
    if any(term in text for term in ["搜索", "查询"]):
        groups["搜索"] = ["搜索", "search", "query", "keyword"]
    if any(term in text for term in ["新增", "添加", "新建"]):
        groups["新增"] = ["新增", "添加", "add", "create", "push"]
    if any(term in text for term in ["保存", "编辑", "备注"]):
        groups["编辑保存"] = ["保存", "编辑", "备注", "save", "edit", "note", "input", "textarea"]
    if any(term in text for term in ["联动", "切换", "点击", "选中"]):
        groups["联动"] = ["选中", "切换", "点击", "selected", "active", "current", "onClick", "@click"]
    if any(term in text for term in ["统计", "指标", "图", "趋势", "概览"]):
        groups["统计"] = ["统计", "指标", "趋势", "total", "count", "chart", "summary"]
    if any(term in text for term in ["异常", "边界", "空", "缺失", "错误", "越权"]):
        groups["边界异常"] = ["异常", "边界", "空", "缺失", "错误", "越权", "empty", "error", "invalid", "disabled"]
    return groups


def code_specific_findings(by_rel: dict[str, str]) -> tuple[list[str], list[str]]:
    issue_groups: dict[str, list[str]] = {
        "placeholder": [],
        "empty_event": [],
        "empty_catch": [],
        "empty_function": [],
    }
    warning_groups: dict[str, list[str]] = {
        "hash_link": [],
        "browser_dialog": [],
        "debug_output": [],
        "button_without_handler": [],
    }
    unfinished_re = re.compile(r"(?<![-_\w])(TODO|FIXME)(?![-_\w])|待实现|暂未实现|占位", re.IGNORECASE)
    for rel, text in by_rel.items():
        suffix = Path(rel).suffix.lower()
        if suffix not in SOURCE_EXTS:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if unfinished_re.search(stripped):
                issue_groups["placeholder"].append(f"{rel}:{index} 仍有未完成占位逻辑：{short_text(stripped, 160)}")
            if re.search(r"(@click|onclick|@submit(?:\.prevent)?|onClick)\s*=\s*['\"]\s*['\"]", stripped):
                issue_groups["empty_event"].append(f"{rel}:{index} 事件绑定为空：{short_text(stripped, 160)}")
            if re.search(r"\bcatch\s*\([^)]*\)\s*\{\s*\}", stripped):
                issue_groups["empty_catch"].append(f"{rel}:{index} 异常被空 catch 吞掉，没有给我可见反馈或恢复逻辑。")
            if suffix in {".vue", ".js", ".ts", ".tsx", ".jsx"} and re.search(
                r"(function\s+\w+\s*\([^)]*\)|(?:const|let|var)\s+\w+\s*=\s*(?:\([^)]*\)|\w+)\s*=>)\s*\{\s*\}",
                stripped,
            ):
                issue_groups["empty_function"].append(f"{rel}:{index} 函数体为空：{short_text(stripped, 160)}")
            if re.search(r"href\s*=\s*['\"]#['\"]", stripped):
                warning_groups["hash_link"].append(f"{rel}:{index} 链接仍指向 # 占位：{short_text(stripped, 160)}")
            if re.search(r"\b(alert|confirm|prompt)\s*\(", stripped):
                warning_groups["browser_dialog"].append(f"{rel}:{index} 仍使用浏览器弹窗做交互反馈，体验和可控状态都偏弱。")
            if re.search(r"\bconsole\.(log|debug)\s*\(", stripped):
                warning_groups["debug_output"].append(f"{rel}:{index} 留有调试输出：{short_text(stripped, 160)}")
        if suffix in {".vue", ".html"}:
            for match in re.finditer(r"<button\b([\s\S]*?)</button>", text, re.IGNORECASE):
                raw_button = match.group(0) or ""
                opener = opening_tag_text(raw_button)
                attrs_match = re.match(r"<button\b([\s\S]*)>$", opener, re.IGNORECASE)
                attrs = attrs_match.group(1) if attrs_match else ""
                if any(token in attrs for token in ("@click", "onclick", 'type="submit"', "type='submit'")):
                    continue
                inner = raw_button[len(opener) :]
                inner = re.sub(r"</button>\s*$", "", inner, flags=re.IGNORECASE).strip()
                label = re.sub(r"<[^>]+>", "", inner)
                label = short_text(label.strip() or "未命名按钮", 60)
                warning_groups["button_without_handler"].append(
                    f"{rel}:{line_no(text, match.start())} 按钮“{label}”没有看到点击处理或提交类型，验收时不可操作。"
                )

    issues = [summarize_group(items) for items in issue_groups.values() if items]
    warnings = [summarize_group(items) for items in warning_groups.values() if items]
    return issues[:10], warnings[:8]


def opening_tag_text(html: str) -> str:
    quote = ""
    for index, char in enumerate(html):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == ">":
            return html[: index + 1]
    return html


def changed_file_map(by_rel: dict[str, str], changed_rel: set[str]) -> dict[str, str]:
    if not changed_rel:
        return {}
    return {rel: text for rel, text in by_rel.items() if rel in changed_rel}


def normalize_changed_files(changed_files, root: Path) -> set[str]:
    if not changed_files:
        return set()
    if isinstance(changed_files, str):
        items = re.split(r"[\n,]+", changed_files)
    else:
        items = list(changed_files)
    result: set[str] = set()
    for item in items:
        text = normalize_rel_path(str(item or ""))
        if not text:
            continue
        path = Path(text)
        if path.is_absolute():
            try:
                text = path.relative_to(root).as_posix()
            except ValueError:
                continue
        result.add(text)
    return result


def normalize_rel_path(value: str) -> str:
    text = str(value or "").strip().strip('"').strip("'").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def summarize_group(items: list[str]) -> str:
    if not items:
        return ""
    suffix = f"；同类还有 {len(items) - 1} 处" if len(items) > 1 else ""
    return items[0] + suffix


def short_text(value: str, limit: int = 900) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
