import { PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Space, Tag, Typography, message } from "antd";
import { useState } from "react";
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
};

export function DashboardPage() {
  const [directions, setDirections] = useState("AgentOps 自动作业平台");
  const [busy, setBusy] = useState<"start" | "continue" | "stop" | "">("");
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

  async function runAction(action: "start" | "continue" | "stop") {
    setBusy(action);
    try {
      if (action === "start") {
        const payload = {
          directions: directions
            .split(/[\n,，]+/)
            .map((item) => item.trim())
            .filter(Boolean)
        };
        await api.post("/jobs/start", payload);
        message.success("作业已开始");
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

  const job = current.data?.job || null;
  const round = current.data?.round || null;
  const logs = current.data?.logs || [];
  const status = job?.status || current.data?.status || "idle";
  const preflightData = preflight.data;
  const canStart = !preflight.isLoading && (!preflightData || preflightData.ready);

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
            onChange={(event) => setDirections(event.target.value)}
            placeholder="输入本次要做的作业范围，可以多行填写。"
          />
          <Space wrap>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={busy === "start"}
              disabled={!canStart}
              onClick={() => void runAction("start")}
            >
              开始
            </Button>
            <Button
              icon={<ReloadOutlined />}
              loading={busy === "continue"}
              onClick={() => void runAction("continue")}
            >
              继续
            </Button>
            <Button
              danger
              icon={<PauseCircleOutlined />}
              loading={busy === "stop"}
              onClick={() => void runAction("stop")}
            >
              停止
            </Button>
          </Space>
          {preflightData && !preflightData.ready ? (
            <Alert showIcon type="warning" message={preflightData.summary || "运行前检查未通过"} />
          ) : null}
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
