import { describe, expect, it } from "vitest";

import { buildSecurityHeaders } from "@/lib/security-headers";

const SUPABASE = "https://project.supabase.example";
const API = "https://api.example.test";

function headerMap(options: Parameters<typeof buildSecurityHeaders>[0]) {
  return Object.fromEntries(buildSecurityHeaders(options).map((h) => [h.key, h.value]));
}

function directives(csp: string) {
  return Object.fromEntries(
    csp
      .split(";")
      .map((d) => d.trim())
      .filter(Boolean)
      .map((d) => {
        const [name, ...values] = d.split(/\s+/);
        return [name, values];
      }),
  );
}

describe("buildSecurityHeaders", () => {
  const prod = headerMap({ supabaseUrl: SUPABASE, harnessApiUrl: `${API}/v1/`, dev: false });
  const csp = directives(prod["Content-Security-Policy"]);

  it("lets the page talk only to itself, Supabase and the harness API", () => {
    // The session (with its refresh token) is in localStorage, so connect-src limits where a
    // script injected by a future bug could send it.
    expect(csp["connect-src"]).toEqual(["'self'", SUPABASE, API]);
    expect(csp["default-src"]).toEqual(["'self'"]);
    expect(csp["img-src"]).toEqual(["'self'", "data:", "blob:"]);
  });

  it("blocks framing, plugins, base-tag tricks and form posts to other sites", () => {
    expect(csp["frame-ancestors"]).toEqual(["'none'"]);
    expect(csp["object-src"]).toEqual(["'none'"]);
    expect(csp["base-uri"]).toEqual(["'self'"]);
    expect(csp["form-action"]).toEqual(["'self'"]);
    expect(prod["X-Frame-Options"]).toBe("DENY");
  });

  it("allows no eval and no script host other than the app in production", () => {
    // 'unsafe-inline' stays because the page is static and Next.js inlines its bootstrap scripts
    // (decision W8 in the PR).
    expect(csp["script-src"]).toEqual(["'self'", "'unsafe-inline'"]);
  });

  it("sends only the origin as referrer to other sites, and no MIME sniffing", () => {
    expect(prod["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
    expect(prod["X-Content-Type-Options"]).toBe("nosniff");
  });

  it("adds 'unsafe-eval' only for the dev server (React needs it there)", () => {
    const dev = headerMap({ supabaseUrl: SUPABASE, harnessApiUrl: API, dev: true });
    expect(directives(dev["Content-Security-Policy"])["script-src"]).toContain("'unsafe-eval'");
  });

  it.each([undefined, "", "not a url", "javascript:alert(1)", "https://*.example.com"])(
    "leaves out a missing or unusable URL (%s) instead of widening the policy",
    (bad) => {
      const headers = headerMap({ supabaseUrl: bad, harnessApiUrl: bad, dev: false });
      expect(directives(headers["Content-Security-Policy"])["connect-src"]).toEqual(["'self'"]);
    },
  );
});
