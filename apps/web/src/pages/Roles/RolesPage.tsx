import { SaveOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Col, Form, Input, List, Row, Space, Switch, Tag, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { api } from "../../api/client";

type Role = {
  id: string;
  template_id: string;
  key: string;
  name: string;
  purpose: string;
  rules: string[];
  enabled: boolean;
  model_config_key: string;
};

type RoleFormValues = {
  name: string;
  purpose: string;
  enabled: boolean;
  model_config_key: string;
  rules_text: string;
};

export function RolesPage() {
  const queryClient = useQueryClient();
  const [active, setActive] = useState<Role | null>(null);
  const [messageText, setMessageText] = useState("");
  const [reply, setReply] = useState("");
  const [form] = Form.useForm<RoleFormValues>();
  const roles = useQuery({
    queryKey: ["roles"],
    queryFn: async () => (await api.get<Role[]>("/roles")).data
  });

  useEffect(() => {
    if (active) {
      form.setFieldsValue({
        name: active.name,
        purpose: active.purpose,
        enabled: active.enabled,
        model_config_key: active.model_config_key || "default",
        rules_text: active.rules.join("\n")
      });
    }
  }, [active, form]);

  async function save(values: RoleFormValues) {
    if (!active) return;
    const response = await api.patch(`/roles/${active.key}`, {
      name: values.name,
      purpose: values.purpose,
      enabled: values.enabled,
      model_config_key: values.model_config_key || "default",
      rules: values.rules_text.split(/\n|,/).map((item) => item.trim()).filter(Boolean)
    });
    setActive(response.data);
    message.success("角色已保存");
    await queryClient.invalidateQueries({ queryKey: ["roles"] });
  }

  async function send() {
    if (!active) return;
    const response = await api.post(`/roles/${active.key}/chat`, { message: messageText });
    setReply(JSON.stringify(response.data, null, 2));
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>角色工作台</Typography.Title>
      <Row gutter={16}>
        <Col span={7}>
          <Card title="我的角色">
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
        <Col span={9}>
          <Card title="角色配置">
            {active ? (
              <Form<RoleFormValues> form={form} layout="vertical" onFinish={(values) => void save(values)}>
                <Form.Item name="enabled" label="启用" valuePropName="checked">
                  <Switch />
                </Form.Item>
                <Form.Item name="name" label="角色名称" rules={[{ required: true, message: "请输入角色名称" }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="purpose" label="职责说明">
                  <Input.TextArea rows={4} />
                </Form.Item>
                <Form.Item name="model_config_key" label="模型配置引用">
                  <Input placeholder="default" />
                </Form.Item>
                <Form.Item name="rules_text" label="绑定规则文件">
                  <Input.TextArea rows={6} />
                </Form.Item>
                <Button type="primary" htmlType="submit" icon={<SaveOutlined />}>
                  保存角色
                </Button>
              </Form>
            ) : (
              <Typography.Text>请选择角色</Typography.Text>
            )}
          </Card>
        </Col>
        <Col span={8}>
          <Card title="角色聊天">
            <Space direction="vertical" className="wide">
              <Input.TextArea rows={6} value={messageText} onChange={(event) => setMessageText(event.target.value)} />
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
