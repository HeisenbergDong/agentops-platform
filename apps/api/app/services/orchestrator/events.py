from __future__ import annotations

from typing import Any


def build_display_message(
    stage: str,
    message: str,
    *,
    level: str = "info",
    extra: dict[str, Any] | None = None,
) -> str:
    data = extra or {}
    explicit = data.get("display_message") or data.get("zh_message")
    if explicit:
        return str(explicit).strip()

    stage_key = str(stage)
    if stage_key in {"job_starting"}:
        return "作业已创建，正在准备第 1 轮。"
    if stage_key == "cleaning_old_runtime":
        return "已清理上一次运行遗留的日志、附件、异常和待执行 Worker 命令。"
    if stage_key == "preflight":
        blocking = data.get("blocking") if isinstance(data.get("blocking"), list) else []
        warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
        if blocking:
            return f"运行前检查未通过，需要先处理：{_join_short(blocking)}。"
        if warnings:
            return f"运行前检查可运行，但有提醒项：{_join_short(warnings)}。"
        return "运行前检查已通过，可以开始真实运行。"
    if stage_key == "loading_rules":
        return "用户角色和规则已加载，准备进入调度流程。"
    if stage_key == "generating_prompt":
        return "基于输入范围，提示词角色正在按规则生成提示词。"
    if stage_key == "project_workspace_prepared":
        workspace = str(data.get("workspace_path") or "").strip()
        project = str(data.get("project_name") or "").strip()
        if workspace:
            return f"已为本次作业准备项目目录 {project or ''}：{workspace}"
        return "已为本次作业准备独立项目目录。"
    if stage_key == "prompt_generation_fallback":
        return "提示词角色暂时不可用，系统正在使用内置规则生成可执行提示词。"
    if stage_key == "prompt_ready":
        preview = str(data.get("prompt_preview") or "").strip()
        if preview:
            return f"提示词已生成：{preview}"
        return "提示词已生成，准备通知 Worker。"
    if stage_key == "sending_to_worker":
        worker_id = str(data.get("worker_id") or "").strip()
        suffix = f"（{worker_id}）" if worker_id else ""
        return f"调度角色已通知 Worker{suffix}接收提示词。"
    if stage_key == "worker_command_started":
        command_type = str(data.get("command_type") or "")
        return _worker_command_started_message(command_type, data)
    if stage_key == "worker_command_finished":
        command_type = str(data.get("command_type") or "")
        status = str(data.get("result_status") or "")
        return _worker_command_finished_message(command_type, status, data)
    if stage_key == "prompt_sent":
        return "Worker 已把提示词输入 Trae CN 并发送。"
    if stage_key == "waiting_trae":
        return "Trae CN 正在工作，等待回复结束。"
    if stage_key == "awaiting_continue":
        reason = _recovery_reason_label(str(data.get("recovery_reason") or data.get("reason") or ""))
        attempts = _continue_attempt_text(data)
        if reason:
            return f"当前回复还没有确认收口（{reason}），Worker 正在尝试续写恢复{attempts}。"
        return f"当前回复还没有确认收口，Worker 正在尝试续写恢复{attempts}。"
    if stage_key == "collecting_trace":
        return "Trae CN 回复已稳定，Worker 开始获取对话内容和执行轨迹。"
    if stage_key == "trace_validating":
        if level == "error":
            return "Trae CN 执行轨迹校验失败，后续写入已停止。"
        return "Worker 已获取 Trae CN 回复，正在校验执行轨迹完整性。"
    if stage_key == "session_collected":
        session_id = str(data.get("session_id") or "").strip()
        if session_id:
            return f"Worker 已获取真实 Trae Session ID：{session_id}"
        return "Worker 正在获取真实 Trae Session ID。"
    if stage_key == "session_missing_abort":
        return "没有获取到真实 Trae Session ID，本轮不能提交 GitHub 或写入飞书。"
    if stage_key == "trace_missing_abort":
        return "没有拿到完整 Trae CN 执行轨迹，本轮已停止，避免提交无效结果。"
    if stage_key == "screenshot_capturing":
        attachment = data.get("attachment_id")
        if attachment:
            return "Worker 已保存 Trae CN 截图，并记录为过程附件。"
        return "Worker 正在保存 Trae CN 当前截图。"
    if stage_key == "product_reviewing":
        if "command" in data:
            return f"Worker 正在运行项目检查命令：{_command_text(data.get('command'))}。"
        if "product_review" in data:
            return "Worker 已完成项目静态扫描，正在整理代码和产物问题。"
        return "Worker 正在扫描项目文件并准备运行检查。"
    if stage_key == "browser_accepting":
        url = str(data.get("url") or data.get("browser_url") or "").strip()
        if url:
            return f"Worker 正在打开本地页面做浏览器验收：{url}"
        return "Worker 正在做浏览器验收。"
    if stage_key == "github_submitting":
        remote_url = str(data.get("remote_url") or "").strip()
        if remote_url:
            return f"Worker 正在提交代码，并同步到 GitHub：{remote_url}"
        return "Worker 正在提交代码，并同步到 GitHub。"
    if stage_key == "github_failed_abort":
        return "GitHub 提交失败，本轮已停止，等待处理后重试。"
    if stage_key in {"feishu_preparing", "feishu_writing"}:
        return "正在整理飞书字段和附件，准备写入飞书记录。"
    if stage_key == "feishu_failed_abort":
        return "飞书写入失败，本轮结果没有完成入表。"
    if stage_key == "round_completed":
        return "本轮流程已完成。"
    if stage_key == "project_completed":
        return "当前项目已完成，代码、记录和附件流程已收尾。"
    if stage_key == "stopped":
        return "已收到停止请求，调度和 Worker 正在停止当前动作。"
    if stage_key == "manual_required":
        return f"流程需要人工处理：{_clean_reason(message)}"
    if stage_key == "worker_command_retry":
        return "已重新下发当前 Worker 命令。"
    if stage_key == "worker_stop_command":
        return "已通知绑定的 Worker 停止当前动作。"
    if stage_key == "dissatisfaction_reason":
        reason = str(data.get("reason") or message or "").strip()
        return f"本轮不满意原因已生成：{_clean_reason(reason)}"

    if level in {"warning", "error"}:
        return _clean_reason(message)
    return _clean_reason(message) or "流程状态已更新。"


def _worker_command_started_message(command_type: str, data: dict[str, Any]) -> str:
    if command_type == "send_prompt":
        return "Worker 收到提示词，正在打开 Trae CN，并准备输入提示词。"
    if command_type == "wait_completion":
        return "Worker 正在观察 Trae CN 回复状态，等待生成结束。"
    if command_type == "click_continue":
        return "Worker 正在尝试让 Trae CN 当前回复继续收口。"
    if command_type == "copy_latest_reply":
        return "Worker 正在复制 Trae CN 最新回复和执行轨迹。"
    if command_type == "capture_screenshot":
        return "Worker 正在截取 Trae CN 当前画面。"
    if command_type == "scan_project":
        workspace = str(data.get("workspace_path") or data.get("trae_workspace_path") or "").strip()
        if workspace:
            return f"Worker 正在扫描项目目录：{workspace}"
        return "Worker 正在扫描项目目录。"
    if command_type == "run_command":
        return f"Worker 正在运行项目检查命令：{_command_text(data.get('command'))}。"
    if command_type == "browser_acceptance":
        return "Worker 正在打开本地页面，检查产物是否可用。"
    if command_type == "git_submit":
        return "Worker 正在整理 Git 改动并提交到 GitHub。"
    if command_type == "stop_current_task":
        return "Worker 收到停止命令，正在停止当前动作。"
    return "Worker 已收到调度命令，正在执行。"


def _worker_command_finished_message(command_type: str, status: str, data: dict[str, Any] | None = None) -> str:
    data = data or {}
    ok = status in {"ok", "success", "completed"}
    if command_type == "send_prompt" and ok:
        return "Worker 已完成提示词发送，Trae CN 开始处理。"
    if command_type == "wait_completion" and ok:
        return "Trae CN 回复已结束，Worker 准备获取回复内容。"
    if command_type == "copy_latest_reply" and ok:
        return "Worker 已获取 Trae CN 回复内容。"
    if command_type == "capture_screenshot" and ok:
        return "Worker 已完成截图。"
    if command_type == "scan_project" and ok:
        return "Worker 已完成项目扫描。"
    if command_type == "run_command" and ok:
        return "项目检查命令已执行完成。"
    if command_type == "browser_acceptance" and ok:
        return "浏览器验收已完成。"
    if command_type == "git_submit" and ok:
        return "GitHub 提交流程已完成。"
    if command_type == "click_continue" and ok:
        return _click_continue_finished_message(data)
    if command_type == "wait_completion" and not ok:
        return "Worker 暂时还不能确认 Trae CN 当前回复已完成，调度器会按恢复流程继续观察。"
    if command_type == "copy_latest_reply" and not ok:
        return "Worker 还没有拿到可用的完整 Trae CN 回复轨迹，调度器会继续恢复和重试采集。"
    if command_type == "click_continue" and not ok:
        return "Worker 没能安全完成续写恢复动作，调度器会根据结果判断是否需要人工处理。"
    if ok:
        return "Worker 命令已执行完成。"
    return "Worker 命令执行失败或需要人工处理。"


def _join_short(items: list[Any], limit: int = 3) -> str:
    values = [str(item) for item in items[:limit] if str(item).strip()]
    if len(items) > limit:
        values.append(f"还有 {len(items) - limit} 项")
    return "、".join(values)


def _continue_attempt_text(data: dict[str, Any]) -> str:
    attempts = data.get("continue_attempts")
    max_attempts = data.get("max_continue_attempts")
    try:
        attempt_value = int(attempts)
    except (TypeError, ValueError):
        return ""
    if attempt_value <= 0:
        return ""
    try:
        max_value = int(max_attempts)
    except (TypeError, ValueError):
        max_value = 0
    if max_value > 0:
        return f"（第 {attempt_value}/{max_value} 次）"
    return f"（第 {attempt_value} 次）"


def _recovery_reason_label(reason: str) -> str:
    key = str(reason or "").strip()
    if key.startswith("trae_turn_not_completed"):
        return "Trae 当前回合仍未完成"
    labels = {
        "awaiting_continuation": "回复提示需要继续",
        "awaiting_current_continuation": "当前回合需要继续",
        "service_interrupted": "Trae 回复出现中断信号",
        "no_completed_turn_after_prompt_send": "还没有找到本次提示词后的完成回合",
        "trace_too_short": "复制到的轨迹太短",
        "empty_trace": "没有复制到回复轨迹",
        "missing_tool_trace_markers": "回复轨迹缺少工具执行记录",
        "final_summary_only": "只复制到总结，没有完整执行过程",
        "partial_code_copy": "复制到的是局部代码片段",
        "pending_intervention_visible": "界面仍显示待确认操作",
        "turn_probe_unavailable": "无法读取 Trae 本地回合状态",
    }
    return labels.get(key, key.replace("_", " ") if key else "")


def _click_continue_finished_message(data: dict[str, Any]) -> str:
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    command_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    if not result and command_data:
        result = command_data
    intervention = result.get("intervention") if isinstance(result.get("intervention"), dict) else {}
    action_taken = str(result.get("action_taken") or "").strip()
    mode = str(intervention.get("mode") or result.get("mode") or "")
    button_text = str(result.get("button_text") or "").strip()
    if button_text:
        return f"Worker 已点击 Trae CN 的「{button_text}」按钮，接下来重新等待回复收口。"
    if action_taken == "typed_continue":
        return "Worker 没有确认到可点击的继续按钮，已向 Trae CN 输入“继续”并重新等待回复。"
    if action_taken == "clicked_button":
        return "Worker 已点击诊断到的继续/确认操作，接下来重新等待回复收口。"
    if action_taken == "clicked_visual_target":
        return "Worker 已按界面诊断结果点击可恢复操作，接下来重新等待回复收口。"
    if action_taken == "clicked_primary_fallback":
        return "Worker 未识别到明确按钮，已尝试安全的主操作位置，接下来重新观察 Trae CN。"
    if mode == "continue-text":
        return "Worker 没有确认到可点击的继续按钮，已向 Trae CN 输入“继续”并重新等待回复。"
    if mode in {"click-point", "cache-visual-intervention", "ai-visual-intervention", "visual-intervention"}:
        return "Worker 已按界面诊断结果点击可恢复操作，接下来重新等待回复收口。"
    if mode == "primary-fallback":
        return "Worker 未识别到明确按钮，已尝试安全的主操作位置，接下来重新观察 Trae CN。"
    return "Worker 已完成一次续写恢复尝试，接下来重新等待 Trae CN 回复收口。"


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(item) for item in command)
    return str(command or "").strip() or "未指定命令"


def _clean_reason(value: str) -> str:
    text = " ".join(str(value or "").split())
    replacements = {
        "Job created and initial round prepared.": "作业已创建。",
        "Command processed": "命令已处理。",
        "Command failed": "命令失败。",
        "manual action": "人工处理",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text[:500]
