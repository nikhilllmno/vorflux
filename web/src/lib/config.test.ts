import { describe, expect, it } from "vitest";

import { parsePublicConfig } from "@/lib/config";

// Keys are built at run time so no key-shaped literal sits in the repository.
function jwtWithRole(role: string) {
  const part = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return [part({ alg: "HS256", typ: "JWT" }), part({ role, iss: "supabase" }), "signature"].join(
    ".",
  );
}
const PUBLISHABLE_KEY = ["sb", "publishable", "test0000"].join("_");
const SECRET_KEY = ["sb", "secret", "test0000"].join("_");

const VALID = {
  NEXT_PUBLIC_SUPABASE_URL: "https://project.supabase.example",
  NEXT_PUBLIC_SUPABASE_ANON_KEY: PUBLISHABLE_KEY,
  NEXT_PUBLIC_HARNESS_API_URL: "https://api.example.test/",
};

describe("parsePublicConfig", () => {
  it("accepts a publishable key and trims the trailing slash of URLs", () => {
    expect(parsePublicConfig(VALID)).toEqual({
      ok: true,
      config: {
        supabaseUrl: "https://project.supabase.example",
        supabaseAnonKey: PUBLISHABLE_KEY,
        harnessApiUrl: "https://api.example.test",
      },
    });
  });

  it("accepts a legacy anon JWT", () => {
    const result = parsePublicConfig({
      ...VALID,
      NEXT_PUBLIC_SUPABASE_ANON_KEY: jwtWithRole("anon"),
    });
    expect(result.ok).toBe(true);
  });

  it("names every missing variable and does not invent defaults", () => {
    expect(parsePublicConfig({})).toEqual({
      ok: false,
      problems: [
        "NEXT_PUBLIC_SUPABASE_URL is not set.",
        "NEXT_PUBLIC_SUPABASE_ANON_KEY is not set.",
        "NEXT_PUBLIC_HARNESS_API_URL is not set.",
      ],
    });
  });

  it.each([
    ["a secret key", SECRET_KEY],
    ["a service_role JWT", jwtWithRole("service_role")],
    ["an authenticated user JWT", jwtWithRole("authenticated")],
    ["an unknown value", "not-a-key"],
  ])("refuses %s as the browser key and never echoes it", (_label, key) => {
    const result = parsePublicConfig({ ...VALID, NEXT_PUBLIC_SUPABASE_ANON_KEY: key });
    expect(result.ok).toBe(false);
    expect(JSON.stringify(result)).toContain("NEXT_PUBLIC_SUPABASE_ANON_KEY");
    expect(JSON.stringify(result)).not.toContain(key);
  });

  it.each(["ftp://api.example.test", "api.example.test", "javascript:alert(1)"])(
    "refuses a non-http API URL %s",
    (url) => {
      const result = parsePublicConfig({ ...VALID, NEXT_PUBLIC_HARNESS_API_URL: url });
      expect(result).toEqual({
        ok: false,
        problems: ["NEXT_PUBLIC_HARNESS_API_URL must be an http(s) URL."],
      });
    },
  );

  it.each(["NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_HARNESS_API_URL"] as const)(
    "refuses plain http for a remote host in %s, so no password or token travels in clear text",
    (name) => {
      const result = parsePublicConfig({ ...VALID, [name]: "http://db.example.com" });
      expect(result).toEqual({
        ok: false,
        problems: [`${name} must use https (plain http is allowed only for localhost).`],
      });
    },
  );

  it.each(["http://localhost:8000", "http://127.0.0.1:54321", "http://[::1]:8000"])(
    "allows plain http for the local host %s",
    (url) => {
      const result = parsePublicConfig({
        ...VALID,
        NEXT_PUBLIC_SUPABASE_URL: url,
        NEXT_PUBLIC_HARNESS_API_URL: url,
      });
      expect(result.ok).toBe(true);
    },
  );

  it("does not treat a host that only starts with localhost as local", () => {
    const result = parsePublicConfig({
      ...VALID,
      NEXT_PUBLIC_HARNESS_API_URL: "http://localhost.example.com",
    });
    expect(result.ok).toBe(false);
  });
});
