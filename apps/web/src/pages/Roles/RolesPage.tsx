import { SaveOutlined, SendOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Col, Form, Input, List, Row, Segmented, Select, Space, Switch, Tag, Typography, message } from "antd";
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

type ChatMode = "record_only" | "llm_reply" | "append_rule_note" | "llm_append_rule_note";

type ChatMessage = {
  id: string;
  sender: "user" | "assistant";
  message: string;
  mode: ChatMode;
  target_rule: string;
  created_at: string;
};

type ChatResponse = {
  messages: ChatMessage[];
};

export function RolesPage() {
  const queryClient = useQueryClient();
  const [active, setActive] = useState<Role | null>(null);
  const [messageText, setMessageText] = useState("");
  const [chatMode, setChatMode] = useState<ChatMode>("record_only");
  const [targetRule, setTargetRule] = useState("");
  const [sending, setSending] = useState(false);
  const [form] = Form.useForm<RoleFormValues>();

  const roles = useQuery({
    queryKey: ["roles"],
    queryFn: async () => (await api.get<Role[]>("/roles")).data
  });
  const chat = useQuery({
    queryKey: ["role-chat", active?.key],
    enabled: Boolean(active),
    queryFn: async () => (await api.get<ChatResponse>(`/roles/${encodeURIComponent(active?.key || "")}/chat`)).data
  });

  useEffect(() => {
    if (!active && roles.data?.length) {
      setActive(roles.data[0]);
    }
  }, [active, roles.data]);

  useEffect(() => {
    if (active) {
      form.setFieldsValue({
        name: active.name,
        purpose: active.purpose,
        enabled: active.enabled,
        model_config_key: active.model_config_key || "default",
        rules_text: active.rules.join("\n")
      });
      setTargetRule(active.rules[0] || "");
    }
  }, [active, form]);

  async function save(values: RoleFormValues) {
    if (!active) return;
    const response = await api.patch<Role>(`/roles/${encodeURIComponent(active.key)}`, {
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
    if (!active || !messageText.trim()) return;
    setSending(true);
    try {
      await api.post(`/roles/${encodeURIComponent(active.key)}/chat`, {
        message: messageText,
        mode: chatMode,
        target_rule: chatMode === "append_rule_note" || chatMode === "llm_append_rule_note" ? targetRule : undefined
      });
      message.success(chatMode === "append_rule_note" || chatMode === "llm_append_rule_note" ? "已写入规则" : "已发送");
      setMessageText("");
      await queryClient.invalidateQueries({ queryKey: ["role-chat", active.key] });
      await queryClient.invalidateQueries({ queryKey: ["rules"] });
    } finally {
      setSending(false);
    }
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
                <List.Item
                  onClick={() => setActive(role)}
                  className={active?.key === role.key ? "active-list-item" : "clickable"}
                >
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
          <Card title="角色能力">
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
              <Segmented
                block
                value={chatMode}
                onChange={(value) => setChatMode(value as ChatMode)}
                options={[
                  { label: "记录", value: "record_only" },
                  { label: "AI", value: "llm_reply" },
                  { label: "直写", value: "append_rule_note" },
                  { label: "AI写入", value: "llm_append_rule_note" }
                ]}
              />
              {chatMode === "append_rule_note" || chatMode === "llm_append_rule_note" ? (
                <Select
                  value={targetRule || undefined}
                  placeholder="选择规则文件"
                  onChange={setTargetRule}
                  options={(active?.rules || []).map((item) => ({ label: item, value: item }))}
                />
              ) : null}
              <div className="chat-panel">
                {(chat.data?.messages || []).map((item) => (
                  <div key={item.id} className={`chat-message ${item.sender}`}>
                    <Typography.Text strong>{item.sender === "user" ? "我" : "角色"}</Typography.Text>
                    <Typography.Paragraph>{item.message}</Typography.Paragraph>
                    {item.target_rule ? <Tag>{item.target_rule}</Tag> : null}
                  </div>
                ))}
              </div>
              <Input.TextArea rows={5} value={messageText} onChange={(event) => setMessageText(event.target.value)} />
              <Button
                type="primary"
                icon={<SendOutlined />}
                onClick={send}
                loading={sending}
                disabled={
                  !active ||
                  !messageText.trim() ||
                  ((chatMode === "append_rule_note" || chatMode === "llm_append_rule_note") && !targetRule)
                }
              >
                发送
              </Button>
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
