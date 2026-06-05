// VeloxQuant operator dashboard — React entrypoint.
//
// Wires the @tanstack/react-query client (polling cache over the `api` service)
// and mounts the dashboard shell. See docs/ARCHITECTURE.md §8 (observability /
// operator cockpit).
import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import "./styles.css";

// Polling defaults: the UI is a near-real-time cockpit. We refetch on an
// interval (per-panel) and keep data fresh on window focus/reconnect. Retries
// are bounded so a downed api surfaces quickly rather than silently spinning.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
      retry: 1,
      staleTime: 1_000,
    },
  },
});

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("VeloxQuant UI: #root element not found in index.html");
}

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
