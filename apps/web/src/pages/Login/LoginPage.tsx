import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { useAuth } from "../../auth/AuthContext";

type LoginValues = {
  email: string;
  password: string;
};

export function LoginPage() {
  const { login } = useAuth();
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(values: LoginValues) {
    setError("");
    setSubmitting(true);
    try {
      await login(values.email, values.password);
    } catch {
      setError("账号或密码不正确");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-screen">
      <Card className="login-card">
        <Typography.Title level={3}>AgentOps 登录</Typography.Title>
        <Typography.Paragraph type="secondary">使用管理员或用户账号进入自动化工作台。</Typography.Paragraph>
        {error ? <Alert type="error" showIcon message={error} className="login-alert" /> : null}
        <Form<LoginValues> layout="vertical" onFinish={submit} initialValues={{ email: "admin@agentops.local" }}>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, message: "请输入邮箱" }]}>
            <Input prefix={<UserOutlined />} autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
