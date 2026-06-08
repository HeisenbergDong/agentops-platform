import { CloudSyncOutlined, SaveOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Form, Input, Row, Select, Space, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";

type SettingsResponse = {
  sections: Record<string, any>;
  readiness: {
    complete: boolean;
    missing_required: string[];
    items: Array<{ key: string; label: string; configured: boolean; required: boolean }>;
  };
};

export function SettingsPage() {
  const [form] = Form.useForm();
  const [discovering, setDiscovering] = useState(false);
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: async () => (await api.get<SettingsResponse>("/settings")).data
  });
  const workers = useQuery({
    queryKey: ["workers-for-settings"],
    queryFn: async () => (await api.get("/workers")).data
  });
  const ruleVersions = useQuery({
    queryKey: ["rule-versions-for-settings"],
    queryFn: async () => (await api.get("/rules/versions")).data
  });

  useEffect(() => {
    if (settings.data?.sections) {
      form.setFieldsValue(settings.data.sections);
    }
  }, [form, settings.data?.sections]);

  const readinessText = useMemo(() => {
    const missing = settings.data?.readiness?.missing_required || [];
    return missing.length ? `缺少：${missing.join("、")}` : "必要配置已完成";
  }, [settings.data]);

  async function save(values: any, showMessage = true) {
    await api.put("/settings", values);
    if (showMessage) {
      message.success("配置已保存");
    }
    await settings.refetch();
  }

  async function discoverFeishu() {
    setDiscovering(true);
    try {
      await save(form.getFieldsValue(true), false);
      const response = await api.post("/settings/feishu/discover");
      message.success(response.data?.resources?.message || "飞书授权已验证");
      await settings.refetch();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || "飞书资源获取失败");
    } finally {
      setDiscovering(false);
    }
  }

  const feishuResources = settings.data?.sections?.feishu?.discovered_resources;

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar">
        <Typography.Title level={3}>用户配置</Typography.Title>
        <Tag color={settings.data?.readiness?.complete ? "green" : "orange"}>{readinessText}</Tag>
      </Space>
      <Alert
        showIcon
        type="info"
        message="用户只配置凭证和偏好；仓库、飞书资源、Trae 执行细节由对应角色和 Worker 在流程中自动处理。"
      />
      <Form form={form} layout="vertical" onFinish={(values) => void save(values)}>
        <Card title="模型配置" loading={settings.isLoading}>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name={["model", "base_url"]} label="模型 Base URL">
                <Input placeholder="OpenAI-compatible base URL" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["model", "model_name"]} label="默认模型">
                <Input placeholder="gpt-4.1 / claude / deepseek ..." />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["model", "api_key"]} label="模型 API Key">
                <Input.Password
                  placeholder={settings.data?.sections?.model?.api_key_configured ? settings.data.sections.model.api_key_mask : "保存后不回显"}
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card title="GitHub 凭证">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["github", "token"]} label="GitHub Token">
                <Input.Password
                  placeholder={settings.data?.sections?.github?.token_configured ? settings.data.sections.github.token_mask : "保存后不回显"}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Typography.Paragraph type="secondary">
                仓库地址不在这里配置。GitHub 角色会按任务上下文和规则决定提交目标。
              </Typography.Paragraph>
            </Col>
            <Col span={24}>
              <Form.Item name={["github", "pubkey"]} label="GitHub Pubkey / Deploy Key">
                <Input.TextArea rows={3} placeholder="ssh-ed25519 ..." />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card
          title="飞书授权"
          extra={
            <Button icon={<CloudSyncOutlined />} loading={discovering} onClick={() => void discoverFeishu()}>
              获取飞书资源
            </Button>
          }
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["feishu", "app_id"]} label="飞书 App ID">
                <Input placeholder="cli_xxx" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["feishu", "app_secret"]} label="飞书 App Secret">
                <Input.Password
                  placeholder={
                    settings.data?.sections?.feishu?.app_secret_configured
                      ? settings.data.sections.feishu.app_secret_mask
                      : "保存后不回显"
                  }
                />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Alert
                showIcon
                type={feishuResources ? "success" : "warning"}
                message={feishuResources?.message || "填写 App ID / Secret 后点击获取，系统会验证授权并缓存可自动刷新的访问 token。"}
              />
            </Col>
          </Row>
        </Card>

        <Card title="Webhook">
          <Form.Item name={["webhook", "url"]} label="Webhook 地址">
            <Input placeholder="https://example.com/hook" />
          </Form.Item>
        </Card>

        <Card title="Trae / Worker">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["worker", "worker_id"]} label="关联 Worker">
                <Select
                  allowClear
                  placeholder="选择已注册 Worker"
                  options={(workers.data || []).map((worker: any) => ({
                    label: `${worker.worker_id} / ${worker.machine_name}`,
                    value: worker.worker_id
                  }))}
                  notFoundContent="暂无可关联 Worker"
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["worker", "trae_workspace_path"]} label="Trae 工作目录">
                <Input placeholder="D:\\zdbz_code" />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Typography.Paragraph type="secondary">
                服务端只保存这份关联配置，不直接访问本机路径；后续由 Worker 拉取或接收该配置。
              </Typography.Paragraph>
            </Col>
          </Row>
        </Card>

        <Card title="默认项">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["defaults", "default_rule_version_id"]} label="默认规则版本">
                <Select
                  allowClear
                  placeholder="选择规则版本"
                  options={(ruleVersions.data || []).map((item: any) => ({
                    label: `v${item.version} ${item.name}`,
                    value: item.id
                  }))}
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Button type="primary" htmlType="submit" icon={<SaveOutlined />}>
          保存配置
        </Button>
      </Form>
    </Space>
  );
}
