import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { AppRouter } from "./routes/AppRouter";
import "./styles/app.css";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <QueryClientProvider client={queryClient}>
        <AppRouter />
      </QueryClientProvider>
    </ConfigProvider>
  </React.StrictMode>
);
