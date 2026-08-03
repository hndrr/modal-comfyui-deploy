import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["server/__tests__/**/*.test.ts"],
  },
});
