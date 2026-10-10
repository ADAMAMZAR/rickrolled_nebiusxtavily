import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

// Builds both pages into app/static, which FastAPI serves (the built files are committed, so running Continuum
// needs no Node). `npm run dev` serves the source with hot reload and sends /api to uvicorn on :8000.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: here("../app/static"),
    emptyOutDir: true,
    rollupOptions: { input: { index: here("index.html"), investigate: here("investigate.html") } },
  },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
