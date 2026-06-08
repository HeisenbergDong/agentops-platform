import { useQuery } from "@tanstack/react-query";
import { Button, Card, Col, Input, List, Row, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { api } from "../../api/client";

type Role = {
  id: string;
  key: string;
  name: string;
  purpose: string;
  rules: string[];
  enabled: boolean;
  model_config_key: string;
};

export function RolesPage() {
  const [active, setActive] = useState<Role | null>(null);
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("");
  const roles = useQuery({
    queryKey: ["roles"],
    queryFn: async () => (await api.get<Role[]>("/roles")).data
  });

  async function send() {
    if (!active) return;
    const response = await api.post(`/roles/${active.key}/chat`, { message });
    setReply(JSON.stringify(response.data, null, 2));
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>角色工作台</Typography.Title>
      <Row gutter={16}>
        <Col span={8}>
          <Card title="系统角色模板">
            <List
              loading={roles.isLoading}
              dataSource={roles.data || []}
              renderItem={(role) => (
                <List.Item onClick={() => setActive(role)} className="clickable">
                  <List.Item.Meta
                    title={
                      <Space>
                        <Typography.Text strong>{role.name}</Typography.Text>
                        <Tag color={role.enabled ? "green" : "default"}>{role.enabled ? "启用" : "停用"}</Tag>
                      </Space>
                    }
                    description={role.purpose}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card title="当前模板能力">
            {active ? (
              <Space direction="vertical">
                <Typography.Text strong>{active.name}</Typography.Text>
                <Typography.Paragraph>{active.purpose}</Typography.Paragraph>
                <Typography.Text>模型配置：{active.model_config_key || "default"}</Typography.Text>
                <Typography.Text>规则文件：{active.rules.join(", ")}</Typography.Text>
              </Space>
            ) : (
              <Typography.Text>请选择角色模板</Typography.Text>
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title="角色聊天">
            <Space direction="vertical" className="wide">
              <Input.TextArea rows={6} value={message} onChange={(event) => setMessage(event.target.value)} />
              <Button type="primary" onClick={send} disabled={!active}>
                发送
              </Button>
              <pre className="reply-panel">{reply}</pre>
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
