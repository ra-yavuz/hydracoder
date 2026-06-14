import { defineConfig } from "vite";

// Build straight into the package's static dir: the stdlib server serves
// lib/hydracoder/web, so the built UI ships inside the .deb with no node
// at runtime. The build output is committed; rebuild with `npm run build`.
export default defineConfig({
  base: "./",
  build: {
    outDir: "../lib/hydracoder/web",
    emptyOutDir: true,
  },
});
