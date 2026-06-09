import { PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Collapse, Descriptions, Empty, Input, List, Row, Space, Tag, Typography, message } from "antd";
import { useState } from "react";
import { api } from "../../api/client";

type RuntimeLog = {
  id: string;
  level: string;
  stage: string;
  message: string;
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
  attachments?: Array<{
    id: string;
    kind: string;
    filename: string;
    path: string;
    content_type: string;
    size_bytes: number;
    created_at: string;
  }>;
  latest_dissatisfaction?: RuntimeLog | null;
  message?: string;
};

type PreflightCheck = {
  key: string;
  label: string;
  status: "pass" | "warning" | "fail";
  message: string;
  required: boolean;
  details?: Record<string, any>;
};

type PreflightResponse = {
  ready: boolean;
  blocking: string[];
  warnings: string[];
  checks: PreflightCheck[];
  summary: string;
};

export function DashboardPage() {
  const [directions, setDirections] = useState("AgentOps 自动作业平台");
  const [busy, setBusy] = useState<"start" | "continue" | "retry" | "stop" | "">("");
  const current = useQuery({
    queryKey: ["current-job"],
    queryFn: async () => (await api.get<CurrentJobResponse>("/jobs/current")).data,
    refetchInterval: 3000
  });
  const preflight = useQuery({
    queryKey: ["settings-preflight"],
    queryFn: async () => (await api.get<PreflightResponse>("/settings/preflight")).data,
    refetchInterval: 3000
  });

  async function runAction(action: "start" | "continue" | "retry" | "stop") {
    setBusy(action);
    try {
      if (action === "start") {
        const payload = { directions: directions.split(/\n|,/).map((item) => item.trim()).filter(Boolean) };
        await api.post("/jobs/start", payload);
        message.success("作业已开始");
      }
      if (action === "continue") {
        const response = await api.post<CurrentJobResponse>("/jobs/continue");
        message.success(response.data.message || "已请求继续");
      }
      if (action === "retry") {
        const response = await api.post<CurrentJobResponse>("/jobs/retry-worker-command");
        message.success(response.data.message || "已重试当前 Worker 命令");
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

  const status = current.data?.job?.status || current.data?.status || "idle";
  const logs = current.data?.logs || [];
  const workerCommand = current.data?.worker_command;
  const attachments = current.data?.attachments || [];
  const dissatisfaction = current.data?.latest_dissatisfaction;
  const preflightData = preflight.data;
  const canRetryWorkerCommand = ["failed", "manual_required", "cancelled"].includes(workerCommand?.status || "");

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>作业控制台</Typography.Title>
      <Row gutter={16}>
        <Col span={10}>
          <Space direction="vertical" className="wide" size={16}>
            <Card title="主操作">
              <Space direction="vertical" className="wide">
                <Input.TextArea
                  rows={5}
                  value={directions}
                  onChange={(event) => setDirections(event.target.value)}
                  placeholder="输入项目方向，可多行"
                />
                <Space>
                  <Button
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    loading={busy === "start"}
                    disabled={preflight.isLoading || (!!preflightData && !preflightData.ready)}
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
                <Alert
                  showIcon
                  type={preflightData && !preflightData.ready ? "warning" : "info"}
                  message={
                    preflightData?.summary ||
                    "开始会清理旧运行日志、附件、错误和待执行 Worker 命令；继续会保留现有作业状态；停止会标记作业停止并通知绑定 Worker。"
                  }
                />
              </Space>
            </Card>
            <Card
              title="真实运行前清单"
              extra={
                preflightData ? (
                  <Tag color={preflightData.ready ? "green" : "red"}>{preflightData.ready ? "可运行" : "需处理"}</Tag>
                ) : (
                  <Tag>加载中</Tag>
                )
              }
            >
              {preflightData?.checks?.length ? (
                <List
                  size="small"
                  dataSource={preflightData.checks}
                  renderItem={(item) => (
                    <List.Item>
                      <Space direction="vertical" size={0} className="wide">
                        <Space>
                          <Tag color={preflightColor(item.status)}>{preflightLabel(item.status)}</Tag>
                          <Typography.Text strong>{item.label}</Typography.Text>
                          {!item.required ? <Tag>可选</Tag> : null}
                        </Space>
                        <Typography.Text type={item.status === "pass" ? "secondary" : "warning"}>
                          {item.message}
                        </Typography.Text>
                        {hasDetails(item.details) ? (
                          <Collapse
                            ghost
                            size="small"
                            items={[
                              {
                                key: item.key,
                                label: "详情",
                                children: <pre>{formatJson(item.details)}</pre>
                              }
                            ]}
                          />
                        ) : null}
                      </Space>
                    </List.Item>
                  )}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待清单加载" />
              )}
            </Card>
            <Card title="当前作业">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="状态">
                  <Tag color={status === "stopped" ? "red" : status === "idle" ? "default" : "blue"}>{status}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="Job ID">{current.data?.job?.id || "-"}</Descriptions.Item>
                <Descriptions.Item label="方向">
                  {(current.data?.job?.directions || []).join(", ") || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="轮次">
                  {current.data?.round ? `第 ${current.data.round.round_index} 轮 / ${current.data.round.status}` : "-"}
                </Descriptions.Item>
                <Descriptions.Item label="Trace">{current.data?.round?.trace_status || "-"}</Descriptions.Item>
                <Descriptions.Item label="GitHub">{current.data?.round?.github_status || "-"}</Descriptions.Item>
                <Descriptions.Item label="飞书">{current.data?.round?.feishu_status || "-"}</Descriptions.Item>
                <Descriptions.Item label="Worker 命令">
                  {current.data?.worker_command
                    ? `${current.data.worker_command.type} / ${current.data.worker_command.status}`
                    : "-"}
                </Descriptions.Item>
                <Descriptions.Item label="Worker ID">
                  {current.data?.worker_command?.worker_id || "-"}
                </Descriptions.Item>
              </Descriptions>
            </Card>
            {workerCommand ? (
              <Card
                title="Worker 命令详情"
                extra={
                  canRetryWorkerCommand ? (
                    <Button
                      size="small"
                      icon={<ReloadOutlined />}
                      loading={busy === "retry"}
                      onClick={() => void runAction("retry")}
                    >
                      重试命令
                    </Button>
                  ) : null
                }
              >
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="Command ID">{workerCommand.command_id}</Descriptions.Item>
                  <Descriptions.Item label="状态">
                    <Tag>{workerCommand.status}</Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="尝试次数">{workerCommand.attempts}</Descriptions.Item>
                  <Descriptions.Item label="错误">{workerCommand.error || "-"}</Descriptions.Item>
                </Descriptions>
                <Collapse
                  size="small"
                  items={[
                    { key: "payload", label: "Payload", children: <pre>{formatJson(workerCommand.payload)}</pre> },
                    { key: "result", label: "Result", children: <pre>{formatJson(workerCommand.result)}</pre> }
                  ]}
                />
              </Card>
            ) : null}
            {current.data?.round?.prompt ? (
              <Card title="当前 Prompt">
                <Input.TextArea rows={10} value={current.data.round.prompt} readOnly spellCheck={false} />
              </Card>
            ) : null}
          </Space>
        </Col>
        <Col span={14}>
          <Space direction="vertical" className="wide" size={16}>
            {dissatisfaction ? (
              <Card title="最新不满意原因">
                <Alert
                  showIcon
                  type="warning"
                  message={dissatisfaction.message}
                  description={<pre>{String(dissatisfaction.extra?.reason || "")}</pre>}
                />
              </Card>
            ) : null}
            <Card title="证据附件">
              {attachments.length ? (
                <List
                  size="small"
                  dataSource={attachments}
                  renderItem={(item) => (
                    <List.Item>
                      <Space direction="vertical" size={0}>
                        <Typography.Text>
                          <Tag>{item.kind}</Tag>
                          {item.filename}
                        </Typography.Text>
                        <Typography.Text type="secondary">
                          {item.path} / {formatBytes(item.size_bytes)}
                        </Typography.Text>
                      </Space>
                    </List.Item>
                  )}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无附件" />
              )}
            </Card>
            <Card title="实时监控日志">
              <div className="log-panel">
                {logs.length ? (
                  logs.map((item) => (
                    <div key={item.id}>
                      {formatTime(item.created_at)} [{item.level}] [{item.stage}] {item.message}
                    </div>
                  ))
                ) : (
                  <div>等待操作</div>
                )}
              </div>
            </Card>
          </Space>
        </Col>
      </Row>
    </Space>
  );
}

function formatTime(value: string): string {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString();
}

function formatJson(value: Record<string, any> | undefined): string {
  return JSON.stringify(value || {}, null, 2);
}

function formatBytes(value: number): string {
  if (!value) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function preflightColor(status: PreflightCheck["status"]): string {
  if (status === "pass") return "green";
  if (status === "warning") return "orange";
  return "red";
}

function preflightLabel(status: PreflightCheck["status"]): string {
  if (status === "pass") return "通过";
  if (status === "warning") return "提醒";
  return "阻断";
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

function hasDetails(value: Record<string, any> | undefined): boolean {
  return !!value && Object.keys(value).length > 0;
}
