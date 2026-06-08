import { Card, List, Space, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";

type Rule = {
  name: string;
  path: string;
  size: number;
};

export function RulesPage() {
  const rules = useQuery({
    queryKey: ["rules"],
    queryFn: async () => (await api.get<Rule[]>("/rules")).data
  });

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>规则中心</Typography.Title>
      <Card title="规则文件">
        <List
          dataSource={rules.data || []}
          renderItem={(rule) => (
            <List.Item>
              <List.Item.Meta title={rule.name} description={`${rule.size} bytes`} />
            </List.Item>
          )}
        />
      </Card>
    </Space>
  );
}
