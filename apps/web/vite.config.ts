import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// HMR config is environment-aware: when reachable through traefik on a
// public domain (`POLARIS_DOMAIN`), the browser must talk wss://… on
// 443 because traefik terminates TLS upstream of vite.  In a "no domain"
// scenario (vite reached directly on localhost:5173), default vite HMR
// over the same origin works.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "../..", ["POLARIS_"]);
  const domain = env.POLARIS_DOMAIN ?? "";
  const hmr = domain
    ? { host: domain, protocol: "wss" as const, clientPort: 443 }
    : true; // default: same origin, no rewrite
  return {
    plugins: [tailwindcss(), react()],
    envDir: "../..",
    server: {
      host: "0.0.0.0", // bind on all interfaces so it works inside a container
      allowedHosts: true,
      hmr,
    },
  };
});
