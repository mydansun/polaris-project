import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [tailwindcss(), react()],
  envDir: "../..",
  server: {
    allowedHosts: true,
    hmr: {
      host: "polaris-dev.xyz",
      protocol: "wss",
      clientPort: 443,
    },
  },
});

