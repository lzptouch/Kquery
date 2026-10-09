import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { installTelemetry } from "./lib/telemetry";
import "./theme.css";

// 埋点必须在渲染前装好：首屏的 page_view 与 LCP 是最容易被漏掉的两条数据
installTelemetry();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
