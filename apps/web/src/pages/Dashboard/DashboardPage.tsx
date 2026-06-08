import { PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Col, Descriptions, Input, Row, Space, Typography } from "antd";
import { useState } from "react";
import { api } from "../../api/client";

export function DashboardPage() {
  const [directions, setDirections] = useState("AgentOps 自动作业平台");
  const [logs, setLogs] = useState<string[]>(["等待操作"]);
  const current = useQuery({
    queryKey: ["current-job"],
    queryFn: async () => (await api.get("/jobs/current")).data,
    refetchInterval: 5000
  });

  async function startJob() {
    const payload = { directions: directions.split(/\n|,/).map((item) => item.trim()).filter(Boolean) };
    const response = await api.post("/jobs/start", payload);
    setLogs((prev) => [...prev, ...formatLogs(response.data)]);
    await current.refetch();
  }

  async function continueJob() {
    const response = await api.post("/jobs/continue");
    setLogs((prev) => [...prev, ...formatLogs(response.data)]);
    await current.refetch();
  }

  async function stopJob() {
    const response = await api.post("/jobs/stop");
    setLogs((prev) => [...prev, ...formatLogs(response.data)]);
    await current.refetch();
  }

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>作业控制台</Typography.Title>
      <Row gutter={16}>
        <Col span={10}>
          <Space direction="vertical" className="wide" size={16}>
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
                  <Button danger icon={<PauseCircleOutlined />} onClick={stopJob}>
                    停止
                  </Button>
                </Space>
              </Space>
            </Card>
            <Card title="当前作业">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="状态">{current.data?.status || "idle"}</Descriptions.Item>
                <Descriptions.Item label="Job ID">{current.data?.job?.id || "-"}</Descriptions.Item>
                <Descriptions.Item label="方向">
                  {(current.data?.job?.directions || []).join(", ") || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="轮次">
                  {current.data?.round ? `第 ${current.data.round.round_index} 轮` : "-"}
                </Descriptions.Item>
              </Descriptions>
            </Card>
          </Space>
        </Col>
        <Col span={14}>
          <Card title="实时监控日志">
            <div className="log-panel">
              {[...formatLogs(current.data), ...logs].map((line, index) => (
                <div key={`${index}-${line}`}>{line}</div>
              ))}
            </div>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}

function formatLogs(data: any): string[] {
  if (!data?.logs?.length) return [];
  return data.logs.map((item: any) => `[${item.stage}] ${item.message}`);
}
