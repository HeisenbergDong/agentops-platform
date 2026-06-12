import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  RedoOutlined,
  ReloadOutlined
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Input, Modal, Space, Tag, Tooltip, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";

type RuntimeLog = {
  id: string;
  level: string;
  stage: string;
  message: string;
  display_message?: string;
  zh_message?: string;
  extra: Record<string, any>;
  created_at: string;
};

type CurrentJobResponse = {
  status: string;
  job?: {
    id: string;
    status: string;
    directions: string[];
    daily_target: number;
    submitted_count: number;
    satisfied_count: number;
    created_at: string;
    updated_at: string;
  } | null;
  round?: {
    id: string;
    round_index: number;
    status: string;
    prompt: string;
    trae_session_id?: string;
    trae_user_message_id?: string;
    trae_task_id?: string;
    trae_trace_id?: string;
    trace_status: string;
    github_status: string;
    feishu_status: string;
  } | null;
  logs?: RuntimeLog[];
  worker_command?: {
    command_id: string;
    worker_id: string;
    type: string;
    status: string;
    attempts: number;
    message: string;
    error: string;
    payload: Record<string, any>;
    result: Record<string, any>;
    created_at: string;
    updated_at: string;
    claimed_at?: string | null;
    finished_at?: string | null;
  } | null;
  message?: string;
};

type PreflightResponse = {
  ready: boolean;
  blocking: string[];
  warnings: string[];
  summary: string;
  checks: PreflightCheck[];
};

type PreflightCheck = {
  key: string;
  label: string;
  status: "pass" | "warning" | "fail" | string;
  message: string;
  required: boolean;
  details?: Record<string, any>;
};

export function DashboardPage() {
  const navigate = useNavigate();
  const [directions, setDirections] = useState("AgentOps 自动作业平台");
  const [directionsTouched, setDirectionsTouched] = useState(false);
  const [busy, setBusy] = useState<"start" | "continue" | "stop" | "reopen" | "">("");
  const current = useQuery({
    queryKey: ["current-job"],
    queryFn: async () => (await api.get<CurrentJobResponse>("/jobs/current")).data,
    refetchInterval: 2500
  });
  const preflight = useQuery({
    queryKey: ["settings-preflight"],
    queryFn: async () => (await api.get<PreflightResponse>("/settings/preflight")).data,
    refetchInterval: 5000
  });

  const job = current.data?.job || null;
  const round = current.data?.round || null;
  const logs = current.data?.logs || [];
  const status = job?.status || current.data?.status || "idle";
  const currentJobDirections = job?.directions?.join("\n") || "";
  const preflightData = preflight.data;
  const directionsList = parseDirections(directions);
  const hasCurrentJob = Boolean(job);
  const hasActiveWorkerCommand = Boolean(
    current.data?.worker_command && ["queued", "claimed", "running"].includes(current.data.worker_command.status)
  );
  const preflightReady = Boolean(preflightData?.ready);
  const canStart = !preflight.isLoading && preflightReady && directionsList.length > 0;
  const canContinue =
    !preflight.isLoading &&
    preflightReady &&
    hasCurrentJob &&
    RESUMABLE_STATES.has(status) &&
    !hasActiveWorkerCommand;
  const canStop = hasCurrentJob && !isTerminalStatus(status);
  const canReopen = !preflight.isLoading && preflightReady && hasCurrentJob && directionsList.length > 0;

  useEffect(() => {
    if (!directionsTouched && currentJobDirections) {
      setDirections(currentJobDirections);
    }
  }, [directionsTouched, currentJobDirections]);

  async function runAction(action: "start" | "continue" | "stop" | "reopen") {
    setBusy(action);
    try {
      if (action === "start") {
        const payload = {
          directions: parseDirections(directions)
        };
        await api.post("/jobs/start", payload);
        message.success("作业已开始");
      }
      if (action === "reopen") {
        const payload = {
          directions: parseDirections(directions)
        };
        const response = await api.post<CurrentJobResponse>("/jobs/reopen", payload);
        message.success(response.data.message || "已请求重开");
      }
      if (action === "continue") {
        const response = await api.post<CurrentJobResponse>("/jobs/continue");
        message.success(response.data.message || "已请求继续");
      }
      if (action === "stop") {
        const response = await api.post<CurrentJobResponse>("/jobs/stop");
        message.success(response.data.message || "已请求停止");
      }
      await current.refetch();
    } catch (error: any) {
      message.error(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  function handleStartClick() {
    if (hasCurrentJob && !isTerminalStatus(status)) {
      Modal.confirm({
        title: "开始新作业？",
        content: "系统会停止现有作业并创建一条新作业。如果要保留当前作业条目、清空轮次和计数后从第 1 轮重跑，请使用“重开”。",
        okText: "开始新作业",
        cancelText: "取消",
        onOk: () => runAction("start")
      });
      return;
    }
    void runAction("start");
  }

  function handleReopenClick() {
    Modal.confirm({
      title: "重开当前作业？",
      content: hasActiveWorkerCommand
        ? "系统会取消当前 Worker 命令，清空当前作业的轮次、提交/满意计数和运行记录，并按上方最新作业范围从第 1 轮重新开始。"
        : "系统会清空当前作业的轮次、提交/满意计数和运行记录，并按上方最新作业范围从第 1 轮重新开始。",
      okText: "重开",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => runAction("reopen")
    });
  }

  return (
    <Space direction="vertical" size={16} className="page dashboard-simple">
      <div className="dashboard-heading">
        <Typography.Title level={3}>作业控制台</Typography.Title>
        <Space size={8} wrap className="dashboard-status-strip">
          <Typography.Text type="secondary">当前第 {job ? job.submitted_count + 1 : "-"} 条</Typography.Text>
          <Typography.Text type="secondary">第 {round?.round_index || "-"} 轮</Typography.Text>
          <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>
        </Space>
      </div>

      <Card className="dashboard-card control-card" title="作业范围">
        <Space direction="vertical" className="wide" size={12}>
          <Input.TextArea
            rows={6}
            value={directions}
            onChange={(event) => {
              setDirectionsTouched(true);
              setDirections(event.target.value);
            }}
            placeholder="输入本次要做的作业范围，可以多行填写。"
          />
          <Space wrap>
            <Tooltip title={canStart ? "" : startDisabledReason(preflight.isLoading, preflightData, directionsList)}>
              <span>
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  loading={busy === "start"}
                  disabled={!canStart}
                  onClick={handleStartClick}
                >
                  开始
                </Button>
              </span>
            </Tooltip>
            <Tooltip
              title={
                canContinue
                  ? ""
                  : continueDisabledReason(preflight.isLoading, preflightData, status, hasCurrentJob, hasActiveWorkerCommand)
              }
            >
              <span>
                <Button
                  icon={<ReloadOutlined />}
                  loading={busy === "continue"}
                  disabled={!canContinue}
                  onClick={() => void runAction("continue")}
                >
                  继续
                </Button>
              </span>
            </Tooltip>
            <Tooltip title={canStop ? "" : stopDisabledReason(status, hasCurrentJob)}>
              <span>
                <Button
                  danger
                  icon={<PauseCircleOutlined />}
                  loading={busy === "stop"}
                  disabled={!canStop}
                  onClick={() => void runAction("stop")}
                >
                  停止
                </Button>
              </span>
            </Tooltip>
            <Tooltip
              title={
                canReopen ? "" : reopenDisabledReason(preflight.isLoading, preflightData, directionsList, hasCurrentJob)
              }
            >
              <span>
                <Button
                  danger
                  icon={<RedoOutlined />}
                  loading={busy === "reopen"}
                  disabled={!canReopen}
                  onClick={handleReopenClick}
                >
                  重开
                </Button>
              </span>
            </Tooltip>
          </Space>
          <PreflightMiniChecks
            loading={preflight.isLoading}
            data={preflightData}
            onRefresh={() => void preflight.refetch()}
            onJump={(target) => navigate(target)}
          />
        </Space>
      </Card>

      <Card className="dashboard-card process-card" title="全过程反馈日志">
        <div className="process-log-panel">
          {logs.length ? (
            logs.map((item) => (
              <div key={item.id} className={`process-log-line ${item.level}`}>
                <span className="process-log-time">{formatTime(item.created_at)}</span>
                <span className="process-log-message">{item.display_message || item.zh_message || item.message}</span>
              </div>
            ))
          ) : (
            <div className="process-log-empty">等待操作。</div>
          )}
        </div>
      </Card>
    </Space>
  );
}

const RESUMABLE_STATES = new Set([
  "prompt_ready",
  "manual_required",
  "trace_missing_abort",
  "session_missing_abort",
  "github_failed_abort",
  "feishu_failed_abort"
]);

const TERMINAL_STATES = new Set(["idle", "stopped", "project_completed"]);

function parseDirections(value: string): string[] {
  return value
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATES.has(status);
}

function PreflightMiniChecks({
  loading,
  data,
  onRefresh,
  onJump
}: {
  loading: boolean;
  data?: PreflightResponse;
  onRefresh: () => void;
  onJump: (target: string) => void;
}) {
  const checks = data?.checks || [];
  return (
    <div className="preflight-mini">
      <Typography.Text type="secondary" className="preflight-mini-label">
        运行前检查
      </Typography.Text>
      <div className="preflight-mini-icons">
        {checks.length ? (
          checks.map((item) => (
            <Tooltip
              key={item.key}
              title={
                <div className="preflight-tooltip">
                  <div className="preflight-tooltip-title">{item.label}</div>
                  <div>{item.message}</div>
                  <div className="preflight-tooltip-target">{preflightTargetText(item.key)}</div>
                </div>
              }
            >
              <button
                type="button"
                className={`preflight-mini-icon ${preflightClassName(item.status)}`}
                aria-label={`${item.label}：${preflightStatusText(item.status)}`}
                onClick={() => onJump(preflightTarget(item.key))}
              >
                {preflightIcon(item.status)}
              </button>
            </Tooltip>
          ))
        ) : (
          <Tooltip title={loading ? "正在检查运行条件" : "暂无检查结果"}>
            <span className="preflight-mini-icon unknown">
              <QuestionCircleOutlined />
            </span>
          </Tooltip>
        )}
      </div>
      <Button size="small" type="link" icon={<ReloadOutlined />} onClick={onRefresh}>
        刷新
      </Button>
      {data?.summary ? (
        <Typography.Text type={data.ready ? "secondary" : "warning"} className="preflight-mini-summary">
          {data.summary}
        </Typography.Text>
      ) : null}
    </div>
  );
}

function preflightIcon(status: string) {
  if (status === "pass") return <CheckCircleOutlined />;
  if (status === "warning" || status === "fail") return <ExclamationCircleOutlined />;
  return <QuestionCircleOutlined />;
}

function preflightClassName(status: string): string {
  if (status === "pass") return "pass";
  if (status === "warning" || status === "fail") return "warning";
  return "unknown";
}

function preflightStatusText(status: string): string {
  if (status === "pass") return "已通过";
  if (status === "warning") return "提醒";
  if (status === "fail") return "未通过";
  return "未知";
}

function preflightTarget(key: string): string {
  if (key === "worker.status" || key === "worker.capabilities") return "/workers";
  if (key.startsWith("model.")) return "/settings#settings-model";
  if (key.startsWith("github.")) return "/settings#settings-github";
  if (key.startsWith("feishu.")) return "/settings#settings-feishu";
  if (key.startsWith("webhook.")) return "/settings#settings-webhook";
  if (key.startsWith("worker.")) return "/settings#settings-worker";
  if (key.startsWith("defaults.")) return "/settings#settings-defaults";
  return "/settings";
}

function preflightTargetText(key: string): string {
  if (key === "worker.status" || key === "worker.capabilities") return "点击查看 Worker 状态";
  return "点击跳转到对应配置";
}

function startDisabledReason(loading: boolean, data: PreflightResponse | undefined, directions: string[]): string {
  if (loading) return "正在检查运行条件";
  if (!directions.length) return "请先填写作业范围";
  if (!data?.ready) return data?.summary || "运行前检查未通过";
  return "";
}

function continueDisabledReason(
  loading: boolean,
  data: PreflightResponse | undefined,
  status: string,
  hasCurrentJob: boolean,
  hasActiveWorkerCommand: boolean
): string {
  if (loading) return "正在检查运行条件";
  if (!hasCurrentJob) return "当前没有可继续的作业";
  if (isTerminalStatus(status)) return "当前作业已结束，不能继续";
  if (hasActiveWorkerCommand) return "Worker 命令正在执行中";
  if (!data?.ready) return data?.summary || "运行前检查未通过";
  if (!RESUMABLE_STATES.has(status)) return "当前状态不需要继续";
  return "";
}

function stopDisabledReason(status: string, hasCurrentJob: boolean): string {
  if (!hasCurrentJob) return "当前没有运行中的作业";
  if (isTerminalStatus(status)) return "当前作业已停止或已结束";
  return "";
}

function reopenDisabledReason(
  loading: boolean,
  data: PreflightResponse | undefined,
  directions: string[],
  hasCurrentJob: boolean
): string {
  if (loading) return "正在检查运行条件";
  if (!hasCurrentJob) return "当前没有可重开的作业";
  if (!directions.length) return "请先填写最新作业范围";
  if (!data?.ready) return data?.summary || "运行前检查未通过";
  return "";
}

function formatTime(value: string): string {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function statusColor(status: string): string {
  if (status === "idle") return "default";
  if (status === "stopped") return "red";
  if (status.includes("failed") || status.includes("abort") || status === "manual_required") return "orange";
  if (status === "project_completed" || status === "round_completed") return "green";
  return "blue";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: "空闲",
    job_starting: "启动中",
    cleaning_old_runtime: "清理中",
    loading_rules: "加载规则",
    generating_prompt: "生成提示词",
    prompt_ready: "提示词已生成",
    sending_to_worker: "通知 Worker",
    prompt_sent: "已发送提示词",
    waiting_trae: "等待 Trae",
    awaiting_continue: "等待继续",
    collecting_trace: "获取轨迹",
    trace_validating: "校验轨迹",
    trace_missing_abort: "轨迹缺失",
    first_round_discarded: "首轮作废",
    session_missing_abort: "Session 缺失",
    screenshot_capturing: "截图中",
    product_reviewing: "检查产物",
    browser_accepting: "浏览器验收",
    github_submitting: "提交 GitHub",
    feishu_writing: "写入飞书",
    feishu_failed_abort: "飞书失败",
    round_completed: "本轮完成",
    project_completed: "项目完成",
    stopped: "已停止",
    manual_required: "需人工处理"
  };
  return labels[status] || status || "未知";
}

function errorMessage(error: any): string {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) {
    const blocking = detail?.preflight?.blocking;
    if (Array.isArray(blocking) && blocking.length) {
      return `${detail.message} 阻断项：${blocking.join("、")}`;
    }
    return String(detail.message);
  }
  return "操作失败";
}
