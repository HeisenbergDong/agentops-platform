import { Card, List, Space, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

export function WorkersPage() {
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: async () => (await api.get("/workers")).data,
    refetchInterval: 5000
  });

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>Worker 管理</Typography.Title>
      <Card title="在线 Worker">
        <List
          dataSource={workers.data || []}
          renderItem={(worker: any) => (
            <List.Item>
              <List.Item.Meta
                title={worker.worker_id}
                description={`${worker.machine_name} / ${worker.current_stage} / ${worker.current_window_title || "-"}`}
              />
              <Tag color={worker.busy ? "orange" : "green"}>{worker.busy ? "忙碌" : "空闲"}</Tag>
            </List.Item>
          )}
        />
      </Card>
    </Space>
  );
}
