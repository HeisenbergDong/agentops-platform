import { SaveOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Col, Form, Input, Row, Space, Typography, message } from "antd";
import { useEffect } from "react";
import { api } from "../../api/client";

export function SettingsPage() {
  const [form] = Form.useForm();
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: async () => (await api.get("/settings")).data
  });

  useEffect(() => {
    if (settings.data) {
      form.setFieldsValue(settings.data);
    }
  }, [form, settings.data]);

  async function save(values: any) {
    await api.put("/settings", values);
    message.success("配置已保存");
    await settings.refetch();
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>用户配置</Typography.Title>
      <Card loading={settings.isLoading}>
        <Form form={form} layout="vertical" onFinish={save}>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["model", "base_url"]} label="模型 Base URL">
                <Input placeholder="OpenAI-compatible base URL" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["model", "api_key"]} label="模型 API Key">
                <Input.Password placeholder={settings.data?.model?.api_key_configured ? settings.data.model.api_key_mask : "保存后不回显"} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["github", "repo_url"]} label="GitHub 仓库地址">
                <Input placeholder="https://github.com/owner/repo.git" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["github", "token"]} label="GitHub Token">
                <Input.Password placeholder={settings.data?.github?.token_configured ? settings.data.github.token_mask : "保存后不回显"} />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item name={["github", "pubkey"]} label="GitHub Pubkey / Deploy Key">
                <Input.TextArea rows={3} placeholder="ssh-ed25519 ..." />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["feishu", "app_id"]} label="飞书 App ID">
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["feishu", "app_secret"]} label="飞书 App Secret">
                <Input.Password placeholder={settings.data?.feishu?.app_secret_configured ? settings.data.feishu.app_secret_mask : "保存后不回显"} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["feishu", "base_token"]} label="飞书 Base Token">
                <Input />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["feishu", "table_id"]} label="飞书 Table ID">
                <Input />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["feishu", "view_id"]} label="飞书 View ID">
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["webhook", "url"]} label="Webhook 地址">
                <Input placeholder="https://example.com/hook" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["webhook", "secret"]} label="Webhook Secret">
                <Input.Password placeholder={settings.data?.webhook?.secret_configured ? settings.data.webhook.secret_mask : "保存后不回显"} />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item name={["trae", "workspace_path"]} label="Trae 工作目录">
                <Input placeholder="D:\\zdbz_code" />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" htmlType="submit" icon={<SaveOutlined />}>
            保存配置
          </Button>
        </Form>
      </Card>
    </Space>
  );
}
