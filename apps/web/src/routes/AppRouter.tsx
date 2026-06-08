import {
  AlertOutlined,
  ApartmentOutlined,
  ControlOutlined,
  DashboardOutlined,
  LogoutOutlined,
  RobotOutlined,
  SettingOutlined,
  TeamOutlined,
  UserAddOutlined
} from "@ant-design/icons";
import { Button, Layout, Menu, Space, Spin, Typography } from "antd";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "../auth/AuthContext";
import { AdminUsersPage } from "../pages/Admin/AdminUsersPage";
import { DashboardPage } from "../pages/Dashboard/DashboardPage";
import { ErrorsPage } from "../pages/Errors/ErrorsPage";
import { JobsPage } from "../pages/Jobs/JobsPage";
import { LoginPage } from "../pages/Login/LoginPage";
import { RolesPage } from "../pages/Roles/RolesPage";
import { RulesPage } from "../pages/Rules/RulesPage";
import { SettingsPage } from "../pages/Settings/SettingsPage";
import { WorkersPage } from "../pages/Workers/WorkersPage";

const { Header, Sider, Content } = Layout;

function Shell() {
  const location = useLocation();
  const { user, loading, logout } = useAuth();
  const selected = location.pathname.startsWith("/admin") ? "/admin/users" : `/${location.pathname.split("/")[1] || ""}`;

  if (loading) {
    return (
      <div className="center-screen">
        <Spin />
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  const menuItems = [
    { key: "/", icon: <DashboardOutlined />, label: <Link to="/">作业控制台</Link> },
    { key: "/jobs", icon: <ApartmentOutlined />, label: <Link to="/jobs">任务详情</Link> },
    { key: "/roles", icon: <TeamOutlined />, label: <Link to="/roles">角色工作台</Link> },
    { key: "/rules", icon: <ControlOutlined />, label: <Link to="/rules">规则中心</Link> },
    { key: "/workers", icon: <RobotOutlined />, label: <Link to="/workers">Worker</Link> },
    { key: "/errors", icon: <AlertOutlined />, label: <Link to="/errors">异常中心</Link> },
    { key: "/settings", icon: <SettingOutlined />, label: <Link to="/settings">用户配置</Link> },
    ...(user.role === "admin"
      ? [{ key: "/admin/users", icon: <UserAddOutlined />, label: <Link to="/admin/users">用户管理</Link> }]
      : [])
  ];

  return (
    <Layout className="app-shell">
      <Sider width={232} className="app-sider">
        <div className="brand">AgentOps</div>
        <Menu theme="dark" mode="inline" selectedKeys={[selected]} items={menuItems} />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Typography.Text strong>多角色 LLM + Windows Worker 自动作业平台</Typography.Text>
          <Space>
            <Typography.Text type="secondary">{user.display_name}</Typography.Text>
            <Button icon={<LogoutOutlined />} onClick={logout}>
              退出
            </Button>
          </Space>
        </Header>
        <Content className="app-content">
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/roles" element={<RolesPage />} />
            <Route path="/rules" element={<RulesPage />} />
            <Route path="/workers" element={<WorkersPage />} />
            <Route path="/errors" element={<ErrorsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route
              path="/admin/users"
              element={user.role === "admin" ? <AdminUsersPage /> : <Navigate to="/" replace />}
            />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export function AppRouter() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </AuthProvider>
  );
}
