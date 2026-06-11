import { CloudSyncOutlined, PlusOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Form, Input, List, Modal, Row, Select, Space, Tag, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { selectPopupProps } from "../../components/selectPopup";

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

type CollectValues = {
  source_type: "url" | "text";
  source: string;
};

type RuleChange = {
  rule_name: string;
  title: string;
  reason: string;
  content: string;
};

type CollectProposal = {
  status: string;
  summary: string;
  changes: RuleChange[];
  warnings: string[];
  model?: string;
  wire_api?: string;
};

export function RulesPage() {
  const queryClient = useQueryClient();
  const [activeRule, setActiveRule] = useState<Rule | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [collectOpen, setCollectOpen] = useState(false);
  const [proposal, setProposal] = useState<CollectProposal | null>(null);
  const [form] = Form.useForm<RuleFormValues>();
  const [createForm] = Form.useForm<CreateRuleValues>();
  const [collectForm] = Form.useForm<CollectValues>();

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

  const collectRules = useMutation({
    mutationFn: async (values: CollectValues) => (await api.post<CollectProposal>("/rules/collect", values)).data,
    onSuccess: (data) => {
      setProposal(data);
      message.success("采集提案已生成");
    }
  });

  const applyProposal = useMutation({
    mutationFn: async () => (await api.post("/rules/collect/apply", { changes: proposal?.changes || [] })).data,
    onSuccess: async () => {
      message.success("规则提案已应用");
      setCollectOpen(false);
      setProposal(null);
      await queryClient.invalidateQueries({ queryKey: ["rules"] });
      if (activeRule?.name) {
        await queryClient.invalidateQueries({ queryKey: ["rules", activeRule.name] });
      }
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
              <Space>
                <Button icon={<CloudSyncOutlined />} onClick={() => setCollectOpen(true)}>
                  采集
                </Button>
                <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                  新建
                </Button>
              </Space>
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

      <Modal
        title="采集规则"
        open={collectOpen}
        width={820}
        onCancel={() => {
          setCollectOpen(false);
          setProposal(null);
        }}
        footer={[
          <Button key="cancel" onClick={() => setCollectOpen(false)}>
            关闭
          </Button>,
          <Button key="collect" loading={collectRules.isPending} onClick={() => collectForm.submit()}>
            生成提案
          </Button>,
          <Button
            key="apply"
            type="primary"
            disabled={!proposal?.changes?.length}
            loading={applyProposal.isPending}
            onClick={() => applyProposal.mutate()}
          >
            应用提案
          </Button>
        ]}
      >
        <Space direction="vertical" size={12} className="wide">
          <Form<CollectValues>
            form={collectForm}
            layout="vertical"
            initialValues={{ source_type: "url" }}
            onFinish={(values) => collectRules.mutate(values)}
          >
            <Form.Item name="source_type" label="来源类型">
              <Select
                {...selectPopupProps}
                options={[
                  { label: "在线文档 URL", value: "url" },
                  { label: "直接粘贴文本", value: "text" }
                ]}
              />
            </Form.Item>
            <Form.Item name="source" label="来源内容" rules={[{ required: true, message: "请输入 URL 或文本" }]}>
              <Input.TextArea rows={5} placeholder="https://... 或直接粘贴需求文档" />
            </Form.Item>
          </Form>
          {proposal ? (
            <Space direction="vertical" size={12} className="wide">
              <Alert
                showIcon
                type={proposal.changes.length ? "success" : "warning"}
                message={proposal.summary || "规则采集已完成"}
                description={`模型：${proposal.model || "-"} / ${proposal.wire_api || "-"}`}
              />
              {proposal.warnings?.length ? <Alert showIcon type="warning" message={proposal.warnings.join("；")} /> : null}
              <List
                dataSource={proposal.changes}
                renderItem={(item) => (
                  <List.Item>
                    <List.Item.Meta
                      title={
                        <Space>
                          <Tag color="blue">{item.rule_name}</Tag>
                          <Typography.Text strong>{item.title}</Typography.Text>
                        </Space>
                      }
                      description={
                        <Space direction="vertical" className="wide">
                          {item.reason ? <Typography.Text type="secondary">{item.reason}</Typography.Text> : null}
                          <Input.TextArea value={item.content} rows={5} readOnly spellCheck={false} />
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            </Space>
          ) : null}
        </Space>
      </Modal>
    </Space>
  );
}
