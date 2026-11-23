import { afterEach, describe, expect, it, vi } from "vitest";

import { assertBrowserSafeSupabaseKey, isBrowserSafeKey } from "@/lib/supabase-key";

// Keys are built at run time so no key-shaped literal sits in the repository.
function jwtWithRole(role: string) {
  const part = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return [part({ alg: "HS256", typ: "JWT" }), part({ role }), "signature"].join(".");
}
const SECRET_KEY = ["sb", "secret", "test0000"].join("_");
const PUBLISHABLE_KEY = ["sb", "publishable", "test0000"].join("_");

describe("isBrowserSafeKey", () => {
  it.each([PUBLISHABLE_KEY, jwtWithRole("anon")])("accepts a browser-safe key", (key) => {
    expect(isBrowserSafeKey(key)).toBe(true);
  });

  it.each([SECRET_KEY, jwtWithRole("service_role"), jwtWithRole("authenticated"), "x.y.z", "k"])(
    "refuses %s",
    (key) => {
      expect(isBrowserSafeKey(key)).toBe(false);
    },
  );
});

describe("build-time guard", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("allows an unset key and a publishable key", () => {
    expect(() => assertBrowserSafeSupabaseKey({})).not.toThrow();
    expect(() =>
      assertBrowserSafeSupabaseKey({ NEXT_PUBLIC_SUPABASE_ANON_KEY: PUBLISHABLE_KEY }),
    ).not.toThrow();
  });

  it("stops on a secret key without echoing it", () => {
    let message = "";
    try {
      assertBrowserSafeSupabaseKey({ NEXT_PUBLIC_SUPABASE_ANON_KEY: SECRET_KEY });
    } catch (error) {
      message = (error as Error).message;
    }
    expect(message).toContain("NEXT_PUBLIC_SUPABASE_ANON_KEY");
    expect(message).not.toContain(SECRET_KEY);
  });

  it("next.config.ts refuses to load with a service_role key, so nothing is bundled", async () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", jwtWithRole("service_role"));
    await expect(import("../../next.config")).rejects.toThrow(/NEXT_PUBLIC_SUPABASE_ANON_KEY/);
  });

  it("next.config.ts loads with a publishable key", async () => {
    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", PUBLISHABLE_KEY);
    const config = (await import("../../next.config")).default;
    expect(config.cacheComponents).toBe(true);
  });
});
