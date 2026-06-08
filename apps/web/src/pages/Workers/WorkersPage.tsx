import { Card, List, Space, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

export function WorkersPage() {
  const workers = useQuery({
    queryKey: ["workers"],
    queryFn: async () => (await api.get("/workers")).data
  });

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>Worker 管理</Typography.Title>
      <Card title="在线 Worker">
        <List
          dataSource={workers.data || []}
          renderItem={(worker: any) => (
            <List.Item>
              <List.Item.Meta title={worker.worker_id} description={worker.current_stage} />
            </List.Item>
          )}
        />
      </Card>
    </Space>
  );
}
