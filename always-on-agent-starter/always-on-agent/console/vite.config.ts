import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // `npm run dev`: the API is agentd's (agentd run --fake serves it on 8765).
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } },
  },
});
