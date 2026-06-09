import { PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, Input, Row, Space, Tag, Typography, message } from "antd";
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
    created_at: string;
    updated_at: string;
    claimed_at?: string | null;
    finished_at?: string | null;
  } | null;
  message?: string;
};

export function DashboardPage() {
  const [directions, setDirections] = useState("AgentOps 自动作业平台");
  const [busy, setBusy] = useState<"start" | "continue" | "stop" | "">("");
  const current = useQuery({
    queryKey: ["current-job"],
    queryFn: async () => (await api.get<CurrentJobResponse>("/jobs/current")).data,
    refetchInterval: 3000
  });

  async function runAction(action: "start" | "continue" | "stop") {
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
      if (action === "stop") {
        const response = await api.post<CurrentJobResponse>("/jobs/stop");
        message.success(response.data.message || "已请求停止");
      }
      await current.refetch();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || "操作失败");
    } finally {
      setBusy("");
    }
  }

  const status = current.data?.job?.status || current.data?.status || "idle";
  const logs = current.data?.logs || [];

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
                  type="info"
                  message="开始会清理旧运行日志、附件、错误和待执行 Worker 命令；继续会保留现有作业状态；停止会标记作业停止并通知绑定 Worker。"
                />
              </Space>
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
            {current.data?.round?.prompt ? (
              <Card title="当前 Prompt">
                <Input.TextArea rows={10} value={current.data.round.prompt} readOnly spellCheck={false} />
              </Card>
            ) : null}
          </Space>
        </Col>
        <Col span={14}>
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
        </Col>
      </Row>
    </Space>
  );
}

function formatTime(value: string): string {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString();
}
