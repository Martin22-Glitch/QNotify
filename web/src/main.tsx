import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ApiError } from "./lib/api";
import { startPrefs } from "./lib/prefs";
import { setFavicon } from "./components/Logo";
import "./index.css";

startPrefs();
setFavicon();

// QueryClient 在模块顶层创建一次（不在组件里），避免每次渲染新建缓存
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 3000,
      refetchOnWindowFocus: false, // 前台恢复由 realtime.ts 统一失效，避免重复请求
      retry: (n, e) => !(e instanceof ApiError && (e.status === 401 || e.status === 404)) && n < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
