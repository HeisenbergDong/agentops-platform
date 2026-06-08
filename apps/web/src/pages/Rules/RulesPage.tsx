import { Card, Descriptions, List, Space, Typography } from "antd";
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
  const active = useQuery({
    queryKey: ["active-rule-version"],
    queryFn: async () => (await api.get("/rules/active")).data
  });

  return (
    <Space direction="vertical" size={16} className="page">
      <Typography.Title level={3}>规则中心</Typography.Title>
      <Card title="当前启用版本">
        <Descriptions column={2} size="small">
          <Descriptions.Item label="版本">{active.data?.version || "-"}</Descriptions.Item>
          <Descriptions.Item label="名称">{active.data?.name || "-"}</Descriptions.Item>
          <Descriptions.Item label="文件数">{active.data?.file_count || "-"}</Descriptions.Item>
          <Descriptions.Item label="摘要">{active.data?.summary || "-"}</Descriptions.Item>
        </Descriptions>
      </Card>
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
