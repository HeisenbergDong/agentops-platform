import { Card, Space, Typography } from "antd";

export function JobsPage() {
  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>任务详情</Typography.Title>
      <Card>任务、项目、轮次、Prompt、Trace、Review、GitHub、Feishu 详情将在这里实现。</Card>
    </Space>
  );
}
