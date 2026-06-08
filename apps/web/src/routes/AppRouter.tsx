import {
  AlertOutlined,
  ApartmentOutlined,
  ControlOutlined,
  DashboardOutlined,
  RobotOutlined,
  SettingOutlined,
  TeamOutlined
} from "@ant-design/icons";
import { Layout, Menu, Typography } from "antd";
import { BrowserRouter, Link, Route, Routes, useLocation } from "react-router-dom";
import { DashboardPage } from "../pages/Dashboard/DashboardPage";
import { ErrorsPage } from "../pages/Errors/ErrorsPage";
import { JobsPage } from "../pages/Jobs/JobsPage";
import { RolesPage } from "../pages/Roles/RolesPage";
import { RulesPage } from "../pages/Rules/RulesPage";
import { SettingsPage } from "../pages/Settings/SettingsPage";
import { WorkersPage } from "../pages/Workers/WorkersPage";

const { Header, Sider, Content } = Layout;

function Shell() {
  const location = useLocation();
  const selected = `/${location.pathname.split("/")[1] || ""}`;
  return (
    <Layout className="app-shell">
      <Sider width={232} className="app-sider">
        <div className="brand">AgentOps</div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          items={[
            { key: "/", icon: <DashboardOutlined />, label: <Link to="/">作业控制台</Link> },
            { key: "/jobs", icon: <ApartmentOutlined />, label: <Link to="/jobs">任务详情</Link> },
            { key: "/roles", icon: <TeamOutlined />, label: <Link to="/roles">角色工作台</Link> },
            { key: "/rules", icon: <ControlOutlined />, label: <Link to="/rules">规则中心</Link> },
            { key: "/workers", icon: <RobotOutlined />, label: <Link to="/workers">Worker</Link> },
            { key: "/errors", icon: <AlertOutlined />, label: <Link to="/errors">异常中心</Link> },
            { key: "/settings", icon: <SettingOutlined />, label: <Link to="/settings">用户配置</Link> }
          ]}
        />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Typography.Text strong>多角色 LLM + Windows Worker 自动作业平台</Typography.Text>
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
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export function AppRouter() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
