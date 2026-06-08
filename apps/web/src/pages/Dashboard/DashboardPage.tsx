import { PlayCircleOutlined, ReloadOutlined, StopCircleOutlined } from "@ant-design/icons";
import { Button, Card, Col, Input, Row, Space, Typography } from "antd";
import { useState } from "react";
import { api } from "../../api/client";

export function DashboardPage() {
  const [directions, setDirections] = useState("AgentOps 自动作业平台");
  const [logs, setLogs] = useState<string[]>(["等待操作"]);

  async function startJob() {
    const payload = { directions: directions.split(/\n|,/).map((item) => item.trim()).filter(Boolean) };
    const response = await api.post("/jobs/start", payload);
    setLogs((prev) => [...prev, JSON.stringify(response.data)]);
  }

  async function continueJob() {
    const response = await api.post("/jobs/continue");
    setLogs((prev) => [...prev, JSON.stringify(response.data)]);
  }

  async function stopJob() {
    const response = await api.post("/jobs/stop");
    setLogs((prev) => [...prev, JSON.stringify(response.data)]);
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>作业控制台</Typography.Title>
      <Row gutter={16}>
        <Col span={10}>
          <Card title="主操作">
            <Space direction="vertical" className="wide">
              <Input.TextArea
                rows={5}
                value={directions}
                onChange={(event) => setDirections(event.target.value)}
                placeholder="输入项目方向，可多行"
              />
              <Space>
                <Button type="primary" icon={<PlayCircleOutlined />} onClick={startJob}>
                  开始
                </Button>
                <Button icon={<ReloadOutlined />} onClick={continueJob}>
                  继续
                </Button>
                <Button danger icon={<StopCircleOutlined />} onClick={stopJob}>
                  停止
                </Button>
              </Space>
            </Space>
          </Card>
        </Col>
        <Col span={14}>
          <Card title="实时监控日志">
            <div className="log-panel">
              {logs.map((line, index) => (
                <div key={`${index}-${line}`}>{line}</div>
              ))}
            </div>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
