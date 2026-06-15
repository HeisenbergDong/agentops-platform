import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { api } from "../../api/client";

type AutomationError = {
  id: string;
  job_id?: string | null;
  round_id?: string | null;
  kind: string;
  stage: string;
  message: string;
  details?: Record<string, any>;
  resolved: boolean;
  created_at: string;
};

export function ErrorsPage() {
  const errors = useQuery<AutomationError[]>({
    queryKey: ["automation-errors"],
    queryFn: async () => (await api.get<AutomationError[]>("/errors")).data,
    refetchInterval: 5000
  });

  const columns: ColumnsType<AutomationError> = [
    {
      title: "异常",
      dataIndex: "message",
      render: (_value, record) => (
        <Space direction="vertical" size={2} className="table-main-cell">
          <Typography.Text strong>{record.message}</Typography.Text>
          <Typography.Text type="secondary">
            {record.kind} / {record.stage}
          </Typography.Text>
        </Space>
      )
    },
    {
      title: "状态",
      dataIndex: "resolved",
      width: 100,
      render: (resolved: boolean) => <Tag color={resolved ? "green" : "orange"}>{resolved ? "已处理" : "待处理"}</Tag>
    },
    {
      title: "作业",
      dataIndex: "job_id",
      width: 220,
      render: (value: string) => value || "-"
    },
    {
      title: "轮次",
      dataIndex: "round_id",
      width: 220,
      render: (value: string) => value || "-"
    },
    {
      title: "时间",
      dataIndex: "created_at",
      width: 190,
      render: formatDateTime
    }
  ];

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar">
        <Typography.Title level={3}>异常中心</Typography.Title>
        <Button icon={<ReloadOutlined />} onClick={() => void errors.refetch()}>
          刷新
        </Button>
      </Space>

      <Card title="异常记录">
        <Table
          rowKey="id"
          loading={errors.isLoading}
          columns={columns}
          dataSource={errors.data || []}
          pagination={{ pageSize: 12 }}
          scroll={{ x: 980 }}
          expandable={{
            expandedRowRender: (record) => (
              <pre className="plain-panel compact">{JSON.stringify(record.details || {}, null, 2)}</pre>
            ),
            rowExpandable: (record) => Boolean(record.details && Object.keys(record.details).length)
          }}
        />
      </Card>
    </Space>
  );
}

function formatDateTime(value?: string) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-";
}
