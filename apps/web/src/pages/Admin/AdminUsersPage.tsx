import { PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Form, Input, Modal, Select, Space, Switch, Table, Tag, Typography, message } from "antd";
import { useState } from "react";
import { api } from "../../api/client";
import type { AuthUser } from "../../auth/AuthContext";
import { selectPopupProps } from "../../components/selectPopup";

type CreateUserValues = {
  email: string;
  display_name: string;
  password: string;
  role: "admin" | "user";
};

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<CreateUserValues>();
  const users = useQuery({
    queryKey: ["admin-users"],
    queryFn: async () => (await api.get<AuthUser[]>("/admin/users")).data
  });

  async function createUser(values: CreateUserValues) {
    await api.post("/admin/users", values);
    message.success("用户已创建");
    setOpen(false);
    form.resetFields();
    await queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  }

  async function toggleUser(user: AuthUser, is_active: boolean) {
    await api.patch(`/admin/users/${user.id}`, { is_active });
    await queryClient.invalidateQueries({ queryKey: ["admin-users"] });
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar">
        <Typography.Title level={3}>用户管理</Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          添加用户
        </Button>
      </Space>
      <Card>
        <Table<AuthUser>
          rowKey="id"
          loading={users.isLoading}
          dataSource={users.data || []}
          pagination={false}
          columns={[
            { title: "邮箱", dataIndex: "email" },
            { title: "名称", dataIndex: "display_name" },
            {
              title: "角色",
              dataIndex: "role",
              render: (role) => <Tag color={role === "admin" ? "blue" : "default"}>{role}</Tag>
            },
            {
              title: "状态",
              dataIndex: "is_active",
              render: (active) => <Tag color={active ? "green" : "red"}>{active ? "启用" : "停用"}</Tag>
            },
            {
              title: "操作",
              render: (_, user) => (
                <Switch
                  checked={user.is_active}
                  checkedChildren="启用"
                  unCheckedChildren="停用"
                  onChange={(checked) => void toggleUser(user, checked)}
                />
              )
            }
          ]}
        />
      </Card>
      <Modal title="添加用户" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form<CreateUserValues>
          form={form}
          layout="vertical"
          initialValues={{ role: "user" }}
          onFinish={(values) => void createUser(values)}
        >
          <Form.Item name="email" label="邮箱" rules={[{ required: true, message: "请输入邮箱" }]}>
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="display_name" label="名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="初始密码" rules={[{ required: true, min: 8, message: "至少 8 位" }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select
              {...selectPopupProps}
              options={[
                { label: "普通用户", value: "user" },
                { label: "管理员", value: "admin" }
              ]}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" block>
            创建
          </Button>
        </Form>
      </Modal>
    </Space>
  );
}
