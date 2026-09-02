import { defineConfig } from "vite";

// base './' — ассеты грузятся относительными путями и в dev-сервере, и из exe
// (Tauri отдаёт dist со своего origin). Ничего внешнего: шрифты вшиты.
export default defineConfig({
  base: "./",
  clearScreen: false,
  // resources/SOUL.md лежит выше корня UI: одна конституция на установщик и boot.py.
  server: { port: 5173, strictPort: true, fs: { allow: ["../.."] } },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    target: "chrome120",
    assetsInlineLimit: 0,
  },
});
