import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Cíl API – v samostatných WSL prostředích nastav VITE_API_TARGET na adresu
// API instance (typicky http://localhost:8000).
const API_TARGET = process.env.VITE_API_TARGET || "http://localhost:8000";
// Port webu lze přepsat přes WEB_PORT.
const WEB_PORT = Number(process.env.WEB_PORT) || 5173;

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    host: true,        // 0.0.0.0 – otevřeš z Windows i z jiného distra
    port: WEB_PORT,
    proxy: {
      "/api": API_TARGET,
      "/uploads": API_TARGET,
    },
  },
});
