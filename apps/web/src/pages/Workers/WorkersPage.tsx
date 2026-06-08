import { ApiOutlined, LinkOutlined, PlusOutlined, SendOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Input, List, Select, Space, Tag, Typography, message } from "antd";
import { useState } from "react";
import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";

export function WorkersPage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [latestCode, setLatestCode] = useState("");
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: async () => (await api.get("/workers")).data,
    refetchInterval: 5000
  });
  const users = useQuery({
    queryKey: ["admin-users-for-workers"],
    queryFn: async () => (await api.get("/admin/users")).data,
    enabled: user?.role === "admin"
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
    await api.post(`/workers/${workerId}/commands`, {
      type: "diagnose_ui",
      payload: { source: "web", note: "manual mock command" }
    });
    message.success("测试命令已排队");
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
          renderItem={(worker: any) => (
            <List.Item
              actions={[
                <Button key="mock" icon={<SendOutlined />} onClick={() => void sendMockCommand(worker.worker_id)}>
                  测试命令
                </Button>
              ]}
            >
              <List.Item.Meta
                avatar={<ApiOutlined />}
                title={
                  <Space>
                    <Typography.Text strong>{worker.display_name || worker.worker_id}</Typography.Text>
                    <Tag color={worker.busy ? "orange" : "green"}>{worker.busy ? "忙碌" : "空闲"}</Tag>
                    <Tag>{worker.status}</Tag>
                  </Space>
                }
                description={
                  <Space direction="vertical" size={4}>
                    <Typography.Text type="secondary">{worker.worker_id}</Typography.Text>
                    <Typography.Text type="secondary">
                      {worker.machine_name} / {worker.worker_type} / {worker.version || "-"}
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      {worker.current_stage} / {worker.current_window_title || "-"}
                    </Typography.Text>
                    {user?.role === "admin" ? (
                      <Space>
                        <LinkOutlined />
                        <Select
                          allowClear
                          size="small"
                          placeholder="绑定用户"
                          value={worker.user_id || undefined}
                          style={{ width: 260 }}
                          options={(users.data || []).map((item: any) => ({
                            label: `${item.display_name} (${item.email})`,
                            value: item.id
                          }))}
                          onChange={(value) => void bindWorker(worker.worker_id, value || null)}
                        />
                      </Space>
                    ) : null}
                  </Space>
                }
              />
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="能力">
                  {(worker.capabilities?.length ? worker.capabilities : worker.supported_apps || []).join(", ") || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="最后心跳">{worker.last_seen_at}</Descriptions.Item>
              </Descriptions>
            </List.Item>
          )}
        />
      </Card>
    </Space>
  );
}
