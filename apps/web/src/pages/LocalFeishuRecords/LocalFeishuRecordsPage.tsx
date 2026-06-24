import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Button, Card, Empty, Input, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { api } from "../../api/client";

type LocalFeishuRecord = {
  record_id: string;
  created_at: string;
  fields: Record<string, any>;
  metadata: Record<string, any>;
  job_id?: string;
  round_id?: string;
  source_path: string;
  line_number: number;
};

type LocalFeishuResponse = {
  fields: string[];
  records: LocalFeishuRecord[];
  paths: string[];
};

const columnWidths: Record<string, number> = {
  "Trae Session ID": 260,
  轮次: 96,
  "User Prompt": 360,
  任务类型: 128,
  业务领域: 140,
  修改范围: 140,
  任务是否完成: 130,
  产物及过程是否满意: 160,
  不满意原因: 360,
  github地址: 280,
  "commit id": 220,
  "分支/文件夹": 180,
  日志轨迹: 420,
  "截图（userprompt附件/产物/运行结果/对话）": 340
};

export function LocalFeishuRecordsPage() {
  const [keyword, setKeyword] = useState("");
  const records = useQuery<LocalFeishuResponse>({
    queryKey: ["local-feishu-records"],
    queryFn: async () => (await api.get<LocalFeishuResponse>("/local-feishu-records")).data,
    refetchInterval: 10000
  });

  const fields = records.data?.fields || [];
  const filtered = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    const rows = records.data?.records || [];
    if (!needle) return rows;
    return rows.filter((record) => {
      const haystack = JSON.stringify(
        {
          record_id: record.record_id,
          created_at: record.created_at,
          fields: record.fields,
          metadata: record.metadata
        },
        null,
        0
      ).toLowerCase();
      return haystack.includes(needle);
    });
  }, [keyword, records.data?.records]);

  const columns: ColumnsType<LocalFeishuRecord> = fields.map((field) => ({
    title: field,
    key: field,
    width: columnWidths[field] || 180,
    render: (_value, record) => renderFieldValue(field, record.fields?.[field])
  }));

  return (
    <Space direction="vertical" size={16} className="page">
      <Space className="toolbar" align="start">
        <Space direction="vertical" size={2}>
          <Typography.Title level={3}>本地记录</Typography.Title>
          <Typography.Text type="secondary">
            展示本地 JSONL 写入的飞书兼容记录，表头与飞书多维表字段保持一致。
          </Typography.Text>
        </Space>
        <Space>
          <Input.Search
            allowClear
            placeholder="搜索记录"
            className="local-record-search"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void records.refetch()}>
            刷新
          </Button>
        </Space>
      </Space>

      <Card
        title={
          <Space>
            <span>飞书兼容记录</span>
            <Tag color="blue">{filtered.length}</Tag>
          </Space>
        }
      >
        {records.data?.paths?.length ? (
          <div className="local-record-paths">
            {records.data.paths.map((path) => (
              <Typography.Text key={path} type="secondary">
                {path}
              </Typography.Text>
            ))}
          </div>
        ) : null}
        <Table
          rowKey={(record) => `${record.source_path}:${record.line_number}:${record.record_id}`}
          loading={records.isLoading}
          columns={columns}
          dataSource={filtered}
          pagination={{ pageSize: 10 }}
          scroll={{ x: Math.max(1600, fields.reduce((total, field) => total + (columnWidths[field] || 180), 0)) }}
          locale={{ emptyText: <Empty description="暂无本地记录" /> }}
          expandable={{
            expandedRowRender: (record) => (
              <Space direction="vertical" size={8} className="wide">
                <Typography.Text type="secondary">
                  {record.record_id} / {formatDateTime(record.created_at)} / {record.source_path}:{record.line_number}
                </Typography.Text>
                <pre className="plain-panel compact">
                  {JSON.stringify({ fields: record.fields, metadata: record.metadata }, null, 2)}
                </pre>
              </Space>
            )
          }}
        />
      </Card>
    </Space>
  );
}

function renderFieldValue(field: string, value: any) {
  const text = stringifyField(value);
  if (!text) return <Typography.Text type="secondary">-</Typography.Text>;
  if (field === "任务是否完成") {
    return <Tag color={text.includes("完成") && !text.includes("未完成") ? "green" : "orange"}>{text}</Tag>;
  }
  if (field === "产物及过程是否满意") {
    return <Tag color={text === "满意" ? "green" : "orange"}>{text}</Tag>;
  }
  if (field === "github地址" && /^https?:\/\//.test(text)) {
    return (
      <Typography.Link href={text} target="_blank" rel="noreferrer" className="local-record-cell">
        {shortText(text, 80)}
      </Typography.Link>
    );
  }
  return <Typography.Text className="local-record-cell">{shortText(text, field === "日志轨迹" ? 220 : 160)}</Typography.Text>;
}

function stringifyField(value: any): string {
  if (value === undefined || value === null) return "";
  if (Array.isArray(value)) return value.map((item) => stringifyField(item)).join("\n");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function shortText(value: string, max: number) {
  return value.length > max ? `${value.slice(0, max)}...` : value;
}

function formatDateTime(value?: string) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-";
}
