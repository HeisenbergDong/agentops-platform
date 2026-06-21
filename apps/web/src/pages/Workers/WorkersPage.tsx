import {
  ApiOutlined,
  DeleteOutlined,
  DownloadOutlined,
  LinkOutlined,
  PlusOutlined,
  SendOutlined,
  SettingOutlined
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Input, List, Popconfirm, Row, Select, Space, Tag, Typography, message } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
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
  online?: boolean;
  registered?: boolean;
  capabilities?: string[];
  supported_apps?: string[];
  last_seen_at?: string | null;
  registered_at?: string | null;
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

type WorkerPackageTicket = {
  download_url: string;
  expires_in: number;
  filename: string;
};

const workerGuideSteps = [
  {
    index: "1",
    title: "登录并补全配置",
    description: "使用管理员分配的账号登录，在用户配置里填写模型、GitHub、飞书和 Trae 工作目录等个人配置。"
  },
  {
    index: "2",
    title: "下载并解压 Worker",
    description: "在自己的 Windows 电脑上下载 Worker 安装包，解压到固定目录，电脑上需要能正常打开 Trae CN。"
  },
  {
    index: "3",
    title: "用注册码注册",
    description: "向管理员获取 Worker 注册码，在本机执行注册命令。注册码如果已分配给你，注册后会自动绑定到你的账号。"
  },
  {
    index: "4",
    title: "启动并保持在线",
    description: "注册后启动 Worker，推荐安装登录自启任务。回到本页看到 Worker 在线后，就可以发起作业。"
  }
];

export function WorkersPage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [latestCode, setLatestCode] = useState("");
  const [downloadingWorkerPackage, setDownloadingWorkerPackage] = useState(false);
  const [deletingWorkerId, setDeletingWorkerId] = useState("");
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

  async function deleteWorker(workerId: string) {
    setDeletingWorkerId(workerId);
    try {
      await api.delete(`/admin/workers/${workerId}`);
      message.success("Worker 已删除");
      await queryClient.invalidateQueries({ queryKey: ["workers"] });
      await queryClient.invalidateQueries({ queryKey: ["worker-recent-commands"] });
    } catch (error: any) {
      const detail = error?.response?.data?.detail;
      message.error(typeof detail === "string" ? detail : "删除 Worker 失败");
    } finally {
      setDeletingWorkerId("");
    }
  }

  async function downloadWorkerPackage() {
    if (downloadingWorkerPackage) {
      return;
    }
    setDownloadingWorkerPackage(true);
    try {
      const response = await api.post<WorkerPackageTicket>("/workers/package-ticket");
      window.location.assign(response.data.download_url);
      message.success("Worker 安装包开始下载");
    } catch (error: any) {
      const detail = error?.response?.data?.detail;
      message.error(typeof detail === "string" ? detail : "Worker 安装包暂不可下载，请联系管理员");
    } finally {
      setDownloadingWorkerPackage(false);
    }
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

      <Card
        title="普通用户接入说明"
        extra={
          <Button
            icon={<DownloadOutlined />}
            loading={downloadingWorkerPackage}
            onClick={() => void downloadWorkerPackage()}
          >
            下载 Windows Worker
          </Button>
        }
      >
        <Space direction="vertical" size={14} className="worker-guide">
          <Alert
            showIcon
            type="info"
            message="管理员创建账号后，你需要登录平台补全自己的配置，并在自己的 Windows 电脑上启动 Worker。平台负责下发任务，Worker 负责操作本机 Trae CN、采集日志和截图，再把结果回传到平台。"
          />
          <Row gutter={[12, 12]}>
            {workerGuideSteps.map((item) => (
              <Col xs={24} md={12} xl={6} key={item.title}>
                <div className="worker-guide-step">
                  <div className="worker-guide-index">{item.index}</div>
                  <Typography.Text strong>{item.title}</Typography.Text>
                  <Typography.Paragraph type="secondary">{item.description}</Typography.Paragraph>
                </div>
              </Col>
            ))}
          </Row>
          <div className="worker-guide-command">
            <Typography.Text type="secondary">注册命令示例</Typography.Text>
            <pre>{`agentops-worker.exe register --server-url http://115.190.113.8 --registration-code <管理员提供的注册码> --workspace-root "D:\\agentops-workspace"`}</pre>
          </div>
          <Space wrap>
            <Button icon={<SettingOutlined />}>
              <Link to="/settings#settings-worker">打开 Worker 配置</Link>
            </Button>
            <Button
              icon={<DownloadOutlined />}
              loading={downloadingWorkerPackage}
              onClick={() => void downloadWorkerPackage()}
            >
              下载 Windows Worker 安装包
            </Button>
          </Space>
        </Space>
      </Card>

      {latestCode ? (
        <Alert
          showIcon
          type="success"
          message="Worker 注册码"
          description={<Input value={latestCode} readOnly />}
        />
      ) : null}

      <Card title="Worker 状态">
        <List
          loading={workers.isLoading}
          dataSource={workers.data || []}
          renderItem={(worker) => {
            const capabilityText =
              (worker.capabilities?.length ? worker.capabilities : worker.supported_apps || []).join(", ") || "-";
            const latestCommand = recentCommands.data?.[worker.worker_id]?.[0];
            const online = isWorkerOnline(worker);
            return (
              <List.Item className="worker-list-item">
                <div className="worker-row">
                  <div className="worker-main">
                    <div className="worker-title-row">
                      <ApiOutlined className="worker-icon" />
                      <Typography.Text strong className="worker-name">
                        {worker.display_name || worker.worker_id}
                      </Typography.Text>
                      {workerStatusTag(worker)}
                      {workerActivityTag(worker)}
                      {worker.registered || worker.registered_at ? <Tag color="default">已注册</Tag> : null}
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
                    <Button
                      icon={<SendOutlined />}
                      disabled={!online}
                      onClick={() => void sendMockCommand(worker.worker_id)}
                    >
                      测试命令
                    </Button>
                    {user?.role === "admin" ? (
                      <Popconfirm
                        title="删除这个 Worker？"
                        description="删除后它会从列表消失，旧 token 会失效；历史命令记录会保留。"
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => void deleteWorker(worker.worker_id)}
                      >
                        <Button
                          danger
                          icon={<DeleteOutlined />}
                          disabled={online}
                          loading={deletingWorkerId === worker.worker_id}
                        >
                          删除
                        </Button>
                      </Popconfirm>
                    ) : null}
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

function isWorkerOnline(worker: WorkerRecord) {
  return Boolean(worker.online) || worker.status === "online" || worker.status === "busy";
}

function workerStatusTag(worker: WorkerRecord) {
  if (worker.status === "online") {
    return <Tag color="green">在线</Tag>;
  }
  if (worker.status === "busy") {
    return <Tag color="orange">在线</Tag>;
  }
  if (worker.status === "offline") {
    return <Tag color="red">离线</Tag>;
  }
  if (worker.status === "revoked") {
    return <Tag color="red">已停用</Tag>;
  }
  return <Tag>未知</Tag>;
}

function workerActivityTag(worker: WorkerRecord) {
  if (!isWorkerOnline(worker)) {
    return null;
  }
  return worker.busy || worker.status === "busy" ? <Tag color="orange">忙碌</Tag> : <Tag color="green">空闲</Tag>;
}
