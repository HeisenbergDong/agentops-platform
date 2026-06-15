import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Descriptions, Empty, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { api } from "../../api/client";

type WorkerCommand = {
  command_id: string;
  type: string;
  status: string;
  message?: string;
  error?: string;
  created_at?: string;
  updated_at?: string;
};

type JobSummary = {
  id: string;
  status: string;
  scope_text?: string;
  directions: string[];
  submitted_count: number;
  satisfied_count: number;
  error_count: number;
  round?: {
    id: string;
    round_index: number;
    status: string;
    trace_status: string;
    github_status: string;
    feishu_status: string;
  } | null;
  worker_command?: WorkerCommand | null;
  created_at: string;
  updated_at: string;
};

type CurrentJobResponse = {
  status: string;
  job?: JobSummary | null;
  round?: {
    id: string;
    round_index: number;
    status: string;
    prompt: string;
    trace_status: string;
    github_status: string;
    feishu_status: string;
  } | null;
  worker_command?: WorkerCommand | null;
  attachments?: Array<{
    id: string;
    kind: string;
    filename: string;
    path: string;
    size_bytes: number;
    created_at: string;
  }>;
  logs?: Array<{
    id: string;
    level: string;
    stage: string;
    display_message?: string;
    message: string;
    created_at: string;
  }>;
};

export function JobsPage() {
  const current = useQuery<CurrentJobResponse>({
    queryKey: ["current-job"],
    queryFn: async () => (await api.get<CurrentJobResponse>("/jobs/current")).data,
    refetchInterval: 3000
  });
  const jobs = useQuery<JobSummary[]>({
    queryKey: ["jobs"],
    queryFn: async () => (await api.get<JobSummary[]>("/jobs")).data,
    refetchInterval: 5000
  });

  const currentJob = current.data?.job;
  const currentRound = current.data?.round;
  const latestCommand = current.data?.worker_command;

  const columns: ColumnsType<JobSummary> = [
    {
      title: "作业",
      dataIndex: "scope_text",
      render: (_value, record) => (
        <Space direction="vertical" size={2} className="table-main-cell">
          <Typography.Text strong>{shortText(record.scope_text || record.directions?.[0] || record.id, 86)}</Typography.Text>
          <Typography.Text type="secondary">{record.id}</Typography.Text>
        </Space>
      )
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 130,
      render: (status: string) => <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>
    },
    {
      title: "轮次",
      width: 120,
      render: (_value, record) => (
        <Space direction="vertical" size={2}>
          <Typography.Text>{record.round ? `第 ${record.round.round_index} 轮` : "-"}</Typography.Text>
          <Typography.Text type="secondary">{record.round?.status || "-"}</Typography.Text>
        </Space>
      )
    },
    {
      title: "提交 / 满意",
      width: 120,
      render: (_value, record) => `${record.submitted_count} / ${record.satisfied_count}`
    },
    {
      title: "Worker",
      width: 180,
      render: (_value, record) => (
        <Space direction="vertical" size={2}>
          <Typography.Text>{record.worker_command?.type || "-"}</Typography.Text>
          <Typography.Text type={record.worker_command?.error ? "danger" : "secondary"}>
            {record.worker_command?.status || "-"}
          </Typography.Text>
        </Space>
      )
    },
    {
      title: "异常",
      dataIndex: "error_count",
      width: 90,
      render: (count: number) => <Tag color={count ? "red" : "default"}>{count || 0}</Tag>
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 190,
      render: formatDateTime
    }
  ];

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar">
        <Typography.Title level={3}>任务详情</Typography.Title>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => {
            void current.refetch();
            void jobs.refetch();
          }}
        >
          刷新
        </Button>
      </Space>

      <Card title="当前作业">
        {currentJob ? (
          <Space direction="vertical" size={14} className="wide">
            <Descriptions size="small" column={{ xs: 1, md: 2, xl: 3 }} bordered>
              <Descriptions.Item label="作业状态">
                <Tag color={statusColor(currentJob.status)}>{statusLabel(currentJob.status)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="当前轮次">{currentRound ? `第 ${currentRound.round_index} 轮` : "-"}</Descriptions.Item>
              <Descriptions.Item label="轮次状态">{currentRound?.status || "-"}</Descriptions.Item>
              <Descriptions.Item label="提交 / 满意">
                {currentJob.submitted_count} / {currentJob.satisfied_count}
              </Descriptions.Item>
              <Descriptions.Item label="Trace">{currentRound?.trace_status || "-"}</Descriptions.Item>
              <Descriptions.Item label="GitHub">{currentRound?.github_status || "-"}</Descriptions.Item>
              <Descriptions.Item label="Feishu">{currentRound?.feishu_status || "-"}</Descriptions.Item>
              <Descriptions.Item label="Worker 命令">
                {latestCommand ? `${latestCommand.type} / ${latestCommand.status}` : "-"}
              </Descriptions.Item>
              <Descriptions.Item label="作业 ID">{currentJob.id}</Descriptions.Item>
            </Descriptions>

            <div>
              <Typography.Text type="secondary">原始作业范围</Typography.Text>
              <pre className="plain-panel">{currentJob.scope_text || currentJob.directions?.join("\n") || "-"}</pre>
            </div>

            {currentRound?.prompt ? (
              <div>
                <Typography.Text type="secondary">当前 Prompt</Typography.Text>
                <pre className="plain-panel compact">{currentRound.prompt}</pre>
              </div>
            ) : null}

            <Table
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={current.data?.attachments || []}
              columns={[
                { title: "附件", dataIndex: "filename" },
                { title: "类型", dataIndex: "kind", width: 120 },
                { title: "大小", dataIndex: "size_bytes", width: 120 },
                { title: "时间", dataIndex: "created_at", width: 190, render: formatDateTime }
              ]}
            />

            <div>
              <Typography.Text type="secondary">最近日志</Typography.Text>
              <div className="mini-log-panel">
                {(current.data?.logs || []).slice(-12).map((item) => (
                  <div key={item.id} className={`mini-log-line ${item.level}`}>
                    <span>{formatTime(item.created_at)}</span>
                    <span>{item.display_message || item.message}</span>
                  </div>
                ))}
              </div>
            </div>
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Card>

      <Card title="作业列表">
        <Table
          rowKey="id"
          loading={jobs.isLoading}
          columns={columns}
          dataSource={jobs.data || []}
          pagination={{ pageSize: 10 }}
          scroll={{ x: 980 }}
        />
      </Card>
    </Space>
  );
}

function shortText(value: string, limit: number) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}...` : text || "-";
}

function formatDateTime(value?: string) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-";
}

function formatTime(value?: string) {
  return value ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false }) : "--:--:--";
}

function statusColor(status: string): string {
  if (status === "project_completed" || status === "round_completed") return "green";
  if (status === "paused" || status === "manual_required" || status.includes("abort") || status.includes("failed")) return "orange";
  if (status === "stopped") return "red";
  return "blue";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    paused: "已暂停",
    stopped: "已停止",
    manual_required: "需人工处理",
    waiting_trae: "等待 Trae",
    collecting_trace: "获取轨迹",
    browser_accepting: "浏览器验收",
    github_submitting: "提交 GitHub",
    feishu_writing: "写入飞书",
    project_completed: "项目完成"
  };
  return labels[status] || status || "-";
}
