import { defineConfig } from "vite";

// UI основной программы. base './' — те же файлы живут и в exe (Tauri), и
// за deskapp по HTTP; ui-kit лежит выше корня, поэтому fs.allow.
export default defineConfig({
  base: "./",
  clearScreen: false,
  // В превью API и труба идут на локальный deskapp: те же пути, что в exe.
  server: {
    port: 5174,
    strictPort: true,
    fs: { allow: [".."] },
    proxy: {
      "/api": "http://127.0.0.1:8094",
      "/pair": "http://127.0.0.1:8094",
      "/events": "http://127.0.0.1:8094",
      "/tunnel": { target: "ws://127.0.0.1:8094", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, target: "chrome120", assetsInlineLimit: 0 },
});
