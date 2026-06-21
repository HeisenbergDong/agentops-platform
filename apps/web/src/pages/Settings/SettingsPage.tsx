import { CloudSyncOutlined, SaveOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Form, Input, Row, Select, Space, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../../api/client";
import { selectPopupProps } from "../../components/selectPopup";

type SettingsResponse = {
  sections: Record<string, any>;
  readiness: {
    complete: boolean;
    missing_required: string[];
    items: Array<{ key: string; label: string; configured: boolean; required: boolean }>;
  };
  preflight?: {
    ready: boolean;
    blocking: string[];
    warnings: string[];
    summary: string;
  };
};

export function SettingsPage() {
  const location = useLocation();
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

  useEffect(() => {
    const targetId = location.hash.replace("#", "");
    if (!targetId) return;
    window.setTimeout(() => {
      document.getElementById(targetId)?.scrollIntoView({ block: "start" });
    }, 0);
  }, [location.hash]);

  useEffect(() => {
    function handleFeishuOAuthMessage(event: MessageEvent) {
      if (event.origin !== window.location.origin) return;
      const data = event.data || {};
      if (data.source !== "agentops-feishu-oauth") return;
      setDiscovering(false);
      if (data.status === "success") {
        message.success(data.message || "飞书授权已完成");
        void settings.refetch();
      } else {
        message.error(data.message || "飞书授权失败");
      }
    }
    window.addEventListener("message", handleFeishuOAuthMessage);
    return () => window.removeEventListener("message", handleFeishuOAuthMessage);
  }, [settings]);

  const readinessText = useMemo(() => {
    if (settings.data?.preflight?.summary) {
      return settings.data.preflight.summary;
    }
    const missing = settings.data?.readiness?.missing_required || [];
    return missing.length ? `缺少：${missing.join("、")}` : "必要配置已完成";
  }, [settings.data]);
  const readinessColor = settings.data?.preflight
    ? settings.data.preflight.ready
      ? "green"
      : "red"
    : settings.data?.readiness?.complete
      ? "green"
      : "orange";

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
      const redirectUri = `${window.location.origin}/api/settings/feishu/oauth/callback`;
      const response = await api.post("/settings/feishu/oauth/begin", { redirect_uri: redirectUri });
      const authorizeUrl = response.data?.authorize_url;
      if (!authorizeUrl) {
        throw new Error("后端没有返回飞书授权地址");
      }
      const popup = window.open(authorizeUrl, "agentops-feishu-oauth", "width=920,height=720");
      if (!popup) {
        message.warning("浏览器拦截了飞书授权窗口，请允许弹窗后重试。");
        setDiscovering(false);
        return;
      }
      message.info("请在弹出的飞书窗口完成授权。");
    } catch (error: any) {
      message.error(error?.response?.data?.detail || error?.message || "飞书授权启动失败");
      setDiscovering(false);
    }
  }

  const feishuResources = settings.data?.sections?.feishu?.discovered_resources;
  const configuredWorkerId = String(Form.useWatch(["worker", "worker_id"], form) || "").trim();
  const selectedWorker = useMemo(
    () => (workers.data || []).find((worker: any) => worker.worker_id === configuredWorkerId),
    [configuredWorkerId, workers.data]
  );
  const workerRuntime = selectedWorker?.runtime_status || {};
  const workerOptions = useMemo(() => {
    const options = (workers.data || []).map((worker: any) => ({
      label: `${worker.worker_id} / ${worker.machine_name}`,
      value: worker.worker_id
    }));
    if (configuredWorkerId && !options.some((option: any) => option.value === configuredWorkerId)) {
      return [
        {
          label: `${configuredWorkerId} / unavailable for current user`,
          value: configuredWorkerId,
          disabled: true
        },
        ...options
      ];
    }
    return options;
  }, [configuredWorkerId, workers.data]);

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar">
        <Typography.Title level={3}>用户配置</Typography.Title>
        <Tag color={readinessColor}>{readinessText}</Tag>
      </Space>
      <Alert
        showIcon
        type="info"
        message="用户只配置凭证和偏好；仓库、飞书资源、Trae 执行细节由对应角色和 Worker 在流程中自动处理。"
      />
      <Form className="settings-form" form={form} layout="vertical" onFinish={(values) => void save(values)}>
        <Card id="settings-model" className="settings-card settings-anchor" title="模型配置" loading={settings.isLoading}>
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item name={["model", "provider"]} label="Provider">
                <Input placeholder="OpenAI" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item name={["model", "base_url"]} label="Base URL">
                <Input placeholder="https://api.openai.com" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["model", "api_key"]} label="API Key">
                <Input.Password
                  placeholder={settings.data?.sections?.model?.api_key_configured ? settings.data.sections.model.api_key_mask : "保存后不回显"}
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name={["model", "model_name"]} label="默认模型">
                <Input placeholder="gpt-5.5" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name={["model", "review_model_name"]} label="检查模型">
                <Input placeholder="gpt-5.5" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name={["model", "wire_api"]} label="接口类型">
                <Select
                  {...selectPopupProps}
                  options={[
                    { label: "Responses", value: "responses" },
                    { label: "Chat Completions", value: "chat_completions" }
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name={["model", "reasoning_effort"]} label="推理强度">
                <Select
                  {...selectPopupProps}
                  allowClear
                  options={["minimal", "low", "medium", "high", "xhigh"].map((value) => ({ label: value, value }))}
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card id="settings-github" className="settings-card settings-anchor" title="GitHub 凭证">
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
                仓库地址不在这里配置；GitHub 角色会按任务上下文和规则决定提交目标。
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
          id="settings-feishu"
          className="settings-card settings-anchor"
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
              <Form.Item name={["feishu", "write_url"]} label="写入飞书地址">
                <Input placeholder="https://bcnrsnl3m9wk.feishu.cn/base/...?table=...&view=..." />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["feishu", "app_token"]} label="Base / App Token">
                <Input placeholder="bascn..." />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["feishu", "table_id"]} label="Table ID">
                <Input placeholder="tbl..." />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name={["feishu", "view_id"]} label="View ID">
                <Input placeholder="vew..." />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Alert
                showIcon
                type={feishuResources ? "success" : "warning"}
                message={feishuResources?.message || "填写 App ID / Secret 和写入地址后点击获取，系统会打开飞书授权页，授权成功后缓存可自动刷新的用户访问 token。"}
              />
            </Col>
          </Row>
        </Card>

        <Card id="settings-webhook" className="settings-card settings-anchor" title="Webhook">
          <Form.Item name={["webhook", "url"]} label="Webhook 地址">
            <Input placeholder="https://example.com/hook" />
          </Form.Item>
        </Card>

        <Card id="settings-worker" className="settings-card settings-anchor" title="Trae / Worker">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["worker", "worker_id"]} label="关联 Worker">
                <Select
                  {...selectPopupProps}
                  allowClear
                  placeholder="选择已注册 Worker"
                  options={workerOptions}
                  notFoundContent="暂无可关联 Worker"
                />
              </Form.Item>
              {configuredWorkerId && workerOptions[0]?.value === configuredWorkerId && workerOptions[0]?.disabled ? (
                <Alert
                  showIcon
                  type="warning"
                  message="当前配置关联的 Worker 不属于当前用户或已不可用，请重新注册并选择当前用户自己的 Worker。"
                />
              ) : null}
            </Col>
            <Col span={12}>
              <Form.Item name={["worker", "trae_workspace_path"]} label="Trae 工作目录">
                <Input placeholder="D:\\zdbz_code" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["worker", "trae_exe_path"]} label="Trae 安装路径">
                <Input placeholder="D:\\app\\Trae CN\\Trae CN.exe" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name={["worker", "browser_url"]} label="浏览器验收 URL">
                <Input placeholder="http://localhost:5173" />
              </Form.Item>
            </Col>
            {selectedWorker ? (
              <Col span={24}>
                <Alert
                  showIcon
                  type={workerRuntime.trae_exe_exists ? "success" : "warning"}
                  message={
                    workerRuntime.trae_exe_exists
                      ? `Worker 已应用 Trae 路径：${workerRuntime.trae_exe_resolved_path || workerRuntime.trae_exe_path}`
                      : `Worker 当前找不到 Trae：${workerRuntime.trae_exe_path || "未上报 Trae 路径"}`
                  }
                  description={`工作目录：${workerRuntime.workspace_root || "-"} ${
                    workerRuntime.workspace_root_exists === false ? "（Worker 本机不存在）" : ""
                  }`}
                />
              </Col>
            ) : null}
            <Col span={24}>
              <Typography.Paragraph type="secondary">
                服务端只保存这份关联配置，不直接访问本机路径；Worker 在线心跳后会拉取 Trae 安装路径、工作目录和浏览器验收 URL，并回报本机校验结果。
              </Typography.Paragraph>
            </Col>
          </Row>
        </Card>

        <Card id="settings-defaults" className="settings-card settings-anchor" title="默认项">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name={["defaults", "default_rule_version_id"]} label="默认规则版本">
                <Select
                  {...selectPopupProps}
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

        <div className="settings-action-bar">
          <Button type="primary" htmlType="submit" icon={<SaveOutlined />}>
            保存配置
          </Button>
        </div>
      </Form>
    </Space>
  );
}
