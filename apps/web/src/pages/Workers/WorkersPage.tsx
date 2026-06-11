import { ApiOutlined, LinkOutlined, PlusOutlined, SendOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Input, List, Select, Space, Tag, Typography, message } from "antd";
import { useState } from "react";
import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { selectPopupProps } from "../../components/selectPopup";

type WorkerRecord = {
  worker_id: string;
  display_name?: string;
  machine_name?: string;
  worker_type?: string;
  version?: string;
  current_stage?: string;
  current_window_title?: string;
  busy?: boolean;
  status?: string;
  capabilities?: string[];
  supported_apps?: string[];
  last_seen_at?: string;
  user_id?: string | null;
};

type AdminUser = {
  id: string;
  display_name: string;
  email: string;
};

type WorkerCommandRecord = {
  command_id: string;
  worker_id: string;
  type: string;
  status: string;
  message?: string;
  error?: string;
  updated_at?: string;
  finished_at?: string | null;
};

export function WorkersPage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [latestCode, setLatestCode] = useState("");
  const workers = useQuery<WorkerRecord[]>({
    queryKey: ["workers"],
    queryFn: async () => (await api.get<WorkerRecord[]>("/workers")).data,
    refetchInterval: 5000
  });
  const users = useQuery<AdminUser[]>({
    queryKey: ["admin-users-for-workers"],
    queryFn: async () => (await api.get<AdminUser[]>("/admin/users")).data,
    enabled: user?.role === "admin"
  });
  const recentCommands = useQuery<Record<string, WorkerCommandRecord[]>>({
    queryKey: ["worker-recent-commands", workers.data?.map((worker) => worker.worker_id).join(",") || ""],
    queryFn: async () => {
      const entries = await Promise.all(
        (workers.data || []).map(async (worker) => {
          const response = await api.get<{ commands: WorkerCommandRecord[] }>(
            `/workers/${worker.worker_id}/recent-commands`,
            { params: { limit: 3 } }
          );
          return [worker.worker_id, response.data.commands] as const;
        })
      );
      return Object.fromEntries(entries);
    },
    enabled: Boolean(workers.data?.length),
    refetchInterval: 3000
  });

  async function createRegistrationCode() {
    const response = await api.post("/admin/worker-registration-codes", { expires_minutes: 60 });
    setLatestCode(response.data.registration_code);
    message.success("注册码已生成，仅显示这一次");
  }

  async function bindWorker(workerId: string, userId: string | null) {
    await api.patch(`/admin/workers/${workerId}/bind`, { user_id: userId || null });
    message.success("Worker 绑定已更新");
    await queryClient.invalidateQueries({ queryKey: ["workers"] });
  }

  async function sendMockCommand(workerId: string) {
    const response = await api.post<WorkerCommandRecord>(`/workers/${workerId}/commands`, {
      type: "diagnose_ui",
      payload: { source: "web", note: "manual mock command" }
    });
    message.success(`测试命令已排队：${response.data.command_id.slice(0, 8)}`);
    await queryClient.invalidateQueries({ queryKey: ["worker-recent-commands"] });
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar">
        <Typography.Title level={3}>Worker 管理</Typography.Title>
        {user?.role === "admin" ? (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => void createRegistrationCode()}>
            生成注册码
          </Button>
        ) : null}
      </Space>

      {latestCode ? (
        <Alert
          showIcon
          type="success"
          message="Worker 注册码"
          description={<Input value={latestCode} readOnly />}
        />
      ) : null}

      <Card title="在线 / 已注册 Worker">
        <List
          loading={workers.isLoading}
          dataSource={workers.data || []}
          renderItem={(worker) => {
            const capabilityText =
              (worker.capabilities?.length ? worker.capabilities : worker.supported_apps || []).join(", ") || "-";
            const latestCommand = recentCommands.data?.[worker.worker_id]?.[0];
            return (
              <List.Item className="worker-list-item">
                <div className="worker-row">
                  <div className="worker-main">
                    <div className="worker-title-row">
                      <ApiOutlined className="worker-icon" />
                      <Typography.Text strong className="worker-name">
                        {worker.display_name || worker.worker_id}
                      </Typography.Text>
                      <Tag color={worker.busy ? "orange" : "green"}>{worker.busy ? "忙碌" : "空闲"}</Tag>
                      <Tag>{worker.status || "-"}</Tag>
                    </div>

                    <Space direction="vertical" size={4} className="worker-meta-lines">
                      <Typography.Text type="secondary">{worker.worker_id}</Typography.Text>
                      <Typography.Text type="secondary">
                        {worker.machine_name || "-"} / {worker.worker_type || "-"} / {worker.version || "-"}
                      </Typography.Text>
                      <Typography.Text type="secondary">
                        {worker.current_stage || "-"} / {worker.current_window_title || "-"}
                      </Typography.Text>
                    </Space>

                    {user?.role === "admin" ? (
                      <div className="worker-bind-row">
                        <LinkOutlined />
                        <Select
                          {...selectPopupProps}
                          allowClear
                          size="small"
                          placeholder="绑定用户"
                          value={worker.user_id || undefined}
                          className="worker-bind-select"
                          options={(users.data || []).map((item) => ({
                            label: `${item.display_name} (${item.email})`,
                            value: item.id
                          }))}
                          onChange={(value) => void bindWorker(worker.worker_id, value || null)}
                        />
                      </div>
                    ) : null}
                  </div>

                  <div className="worker-actions">
                    <Button icon={<SendOutlined />} onClick={() => void sendMockCommand(worker.worker_id)}>
                      测试命令
                    </Button>
                  </div>
                </div>

                <div className="worker-detail-grid">
                  <Typography.Text type="secondary" className="worker-detail-label">
                    能力
                  </Typography.Text>
                  <Typography.Text className="worker-detail-value worker-capabilities">{capabilityText}</Typography.Text>
                  <Typography.Text type="secondary" className="worker-detail-label">
                    最后心跳
                  </Typography.Text>
                  <Typography.Text className="worker-detail-value">{worker.last_seen_at || "-"}</Typography.Text>
                  <Typography.Text type="secondary" className="worker-detail-label">
                    最近命令
                  </Typography.Text>
                  <div className="worker-detail-value worker-command-status">
                    {latestCommand ? (
                      <>
                        <Tag color={commandStatusColor(latestCommand.status)}>{latestCommand.status}</Tag>
                        <Typography.Text>{latestCommand.type}</Typography.Text>
                        <Typography.Text type="secondary">
                          {latestCommand.finished_at || latestCommand.updated_at || "-"}
                        </Typography.Text>
                        {latestCommand.error ? (
                          <Typography.Text type="danger">{latestCommand.error}</Typography.Text>
                        ) : latestCommand.message ? (
                          <Typography.Text type="secondary">{latestCommand.message}</Typography.Text>
                        ) : null}
                      </>
                    ) : (
                      <Typography.Text type="secondary">暂无命令</Typography.Text>
                    )}
                  </div>
                </div>
              </List.Item>
            );
          }}
        />
      </Card>
    </Space>
  );
}

function commandStatusColor(status?: string) {
  if (status === "success" || status === "completed") {
    return "green";
  }
  if (status === "failed" || status === "manual_required" || status === "cancelled") {
    return "red";
  }
  if (status === "claimed" || status === "running") {
    return "blue";
  }
  return "default";
}
