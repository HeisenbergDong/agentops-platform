import { PlusOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Col, Form, Input, List, Modal, Row, Space, Tag, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { api } from "../../api/client";

type Rule = {
  id: string;
  name: string;
  size: number;
  source_name: string;
  is_active: boolean;
  updated_at: string;
  created_at: string;
  content?: string;
};

type ActiveRuleVersion = {
  version?: number;
  name?: string;
  summary?: string;
  file_count?: number;
};

type RuleFormValues = {
  content: string;
};

type CreateRuleValues = {
  name: string;
  content: string;
};

export function RulesPage() {
  const queryClient = useQueryClient();
  const [activeRule, setActiveRule] = useState<Rule | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm<RuleFormValues>();
  const [createForm] = Form.useForm<CreateRuleValues>();

  const rules = useQuery({
    queryKey: ["rules"],
    queryFn: async () => (await api.get<Rule[]>("/rules")).data
  });
  const active = useQuery({
    queryKey: ["active-rule-version"],
    queryFn: async () => (await api.get<ActiveRuleVersion>("/rules/active")).data
  });
  const ruleDetail = useQuery({
    queryKey: ["rules", activeRule?.name],
    enabled: Boolean(activeRule),
    queryFn: async () => (await api.get<Rule>(`/rules/${encodeURIComponent(activeRule?.name || "")}`)).data
  });

  useEffect(() => {
    if (!activeRule && rules.data?.length) {
      setActiveRule(rules.data[0]);
    }
  }, [activeRule, rules.data]);

  useEffect(() => {
    if (ruleDetail.data) {
      form.setFieldsValue({ content: ruleDetail.data.content || "" });
      setActiveRule(ruleDetail.data);
    }
  }, [form, ruleDetail.data]);

  const saveRule = useMutation({
    mutationFn: async (values: RuleFormValues) =>
      (await api.put<Rule>(`/rules/${encodeURIComponent(activeRule?.name || "")}`, { content: values.content })).data,
    onSuccess: async (data) => {
      message.success("规则已保存");
      setActiveRule(data);
      await queryClient.invalidateQueries({ queryKey: ["rules"] });
      await queryClient.invalidateQueries({ queryKey: ["rules", data.name] });
    }
  });

  const resetRule = useMutation({
    mutationFn: async () => (await api.post<Rule>(`/rules/${encodeURIComponent(activeRule?.name || "")}/reset`)).data,
    onSuccess: async (data) => {
      message.success("已重置为系统模板");
      setActiveRule(data);
      form.setFieldsValue({ content: data.content || "" });
      await queryClient.invalidateQueries({ queryKey: ["rules"] });
      await queryClient.invalidateQueries({ queryKey: ["rules", data.name] });
    }
  });

  const createRule = useMutation({
    mutationFn: async (values: CreateRuleValues) => (await api.post<Rule>("/rules", values)).data,
    onSuccess: async (data) => {
      message.success("规则已创建");
      setCreateOpen(false);
      createForm.resetFields();
      setActiveRule(data);
      await queryClient.invalidateQueries({ queryKey: ["rules"] });
    }
  });

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>规则中心</Typography.Title>
      <Card title="当前启用版本">
        <Space size={16} wrap>
          <Typography.Text>版本：{active.data?.version || "-"}</Typography.Text>
          <Typography.Text>名称：{active.data?.name || "-"}</Typography.Text>
          <Typography.Text>文件数：{active.data?.file_count || "-"}</Typography.Text>
          <Typography.Text>摘要：{active.data?.summary || "-"}</Typography.Text>
        </Space>
      </Card>
      <Row gutter={16}>
        <Col span={8}>
          <Card
            title="我的规则文件"
            extra={
              <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                新建
              </Button>
            }
          >
            <List
              loading={rules.isLoading}
              dataSource={rules.data || []}
              renderItem={(rule) => (
                <List.Item
                  onClick={() => setActiveRule(rule)}
                  className={activeRule?.name === rule.name ? "active-list-item" : ""}
                >
                  <List.Item.Meta
                    title={
                      <Space>
                        {rule.name}
                        {rule.source_name ? <Tag>系统模板</Tag> : <Tag color="blue">自定义</Tag>}
                      </Space>
                    }
                    description={`${rule.size} bytes`}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>
        <Col span={16}>
          <Card
            title={activeRule?.name || "规则内容"}
            extra={
              <Space>
                <Button
                  icon={<ReloadOutlined />}
                  disabled={!activeRule?.source_name}
                  loading={resetRule.isPending}
                  onClick={() => resetRule.mutate()}
                >
                  重置
                </Button>
                <Button
                  type="primary"
                  icon={<SaveOutlined />}
                  disabled={!activeRule}
                  loading={saveRule.isPending}
                  onClick={() => form.submit()}
                >
                  保存
                </Button>
              </Space>
            }
          >
            {activeRule ? (
              <Form<RuleFormValues> form={form} layout="vertical" onFinish={(values) => saveRule.mutate(values)}>
                <Form.Item name="content">
                  <Input.TextArea rows={24} spellCheck={false} />
                </Form.Item>
              </Form>
            ) : (
              <Typography.Text>请选择规则文件</Typography.Text>
            )}
          </Card>
        </Col>
      </Row>
      <Modal
        title="新建规则文件"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={createRule.isPending}
      >
        <Form<CreateRuleValues> form={createForm} layout="vertical" onFinish={(values) => createRule.mutate(values)}>
          <Form.Item name="name" label="文件名" rules={[{ required: true, message: "请输入文件名" }]}>
            <Input placeholder="custom_rules.md" />
          </Form.Item>
          <Form.Item name="content" label="内容" initialValue="">
            <Input.TextArea rows={8} spellCheck={false} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
