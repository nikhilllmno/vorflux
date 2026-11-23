// next.config.ts runs its guards when Next.js loads it, before anything is bundled, and sets the
// response headers for every page.
import { afterEach, describe, expect, it, vi } from "vitest";

const PUBLISHABLE_KEY = ["sb", "publishable", "test0000"].join("_");

async function loadConfig(env: Record<string, string>) {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", PUBLISHABLE_KEY);
  vi.stubEnv("NEXT_PUBLIC_SUPABASE_URL", "https://project.supabase.example");
  vi.stubEnv("NEXT_PUBLIC_HARNESS_API_URL", "https://api.example.test");
  for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value);
  return (await import("../../next.config")).default;
}

async function pageHeaders(env: Record<string, string> = {}) {
  const config = await loadConfig(env);
  const rules = (await config.headers?.()) ?? [];
  expect(rules.map((rule) => rule.source)).toEqual(["/:path*"]);
  return Object.fromEntries(rules[0].headers.map((h) => [h.key, h.value]));
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("next.config.ts security headers", () => {
  it("sets a CSP that allows only this origin plus the configured Supabase and API origins", async () => {
    const csp = (await pageHeaders())["Content-Security-Policy"];
    expect(csp).toContain(
      "connect-src 'self' https://project.supabase.example https://api.example.test",
    );
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
  });

  it("sets Referrer-Policy and X-Content-Type-Options, and hides X-Powered-By", async () => {
    const headers = await pageHeaders();
    expect(headers["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
    expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    expect((await loadConfig({})).poweredByHeader).toBe(false);
  });
});

describe("next.config.ts URL guard", () => {
  it.each(["NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_HARNESS_API_URL"])(
    "stops the build when %s uses plain http for a remote host",
    async (name) => {
      await expect(loadConfig({ [name]: "http://db.example.com" })).rejects.toThrow(
        `${name} must use https`,
      );
    },
  );

  it.each(["http://localhost:54321", "http://127.0.0.1:8000"])(
    "allows plain http for the local host %s",
    async (url) => {
      const config = await loadConfig({
        NEXT_PUBLIC_SUPABASE_URL: url,
        NEXT_PUBLIC_HARNESS_API_URL: url,
      });
      expect(config.poweredByHeader).toBe(false);
    },
  );

  it("allows unset URLs, so CI can build without configuration", async () => {
    await expect(
      loadConfig({ NEXT_PUBLIC_SUPABASE_URL: "", NEXT_PUBLIC_HARNESS_API_URL: "" }),
    ).resolves.toBeDefined();
  });
});
