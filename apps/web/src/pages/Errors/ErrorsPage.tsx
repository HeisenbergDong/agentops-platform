import { Card, Space, Typography } from "antd";

export function ErrorsPage() {
  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>异常中心</Typography.Title>
      <Card>日志缺失、Worker 断线、GitHub 失败、飞书失败等异常将在这里处理。</Card>
    </Space>
  );
}
