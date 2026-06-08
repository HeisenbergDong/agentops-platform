import { Card, Form, Input, Space, Typography } from "antd";

export function SettingsPage() {
  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>用户配置</Typography.Title>
      <Card>
        <Form layout="vertical">
          <Form.Item label="模型 Base URL">
            <Input placeholder="OpenAI-compatible base URL" />
          </Form.Item>
          <Form.Item label="GitHub 地址">
            <Input placeholder="https://github.com/owner/repo.git" />
          </Form.Item>
          <Form.Item label="飞书 Base Token">
            <Input.Password placeholder="脱敏保存" />
          </Form.Item>
          <Form.Item label="Trae 工作目录">
            <Input placeholder="D:\\code-space\\coding-soler" />
          </Form.Item>
        </Form>
      </Card>
    </Space>
  );
}
