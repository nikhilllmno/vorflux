import { describe, expect, it, vi } from "vitest";

import { createHarnessApi, HarnessApiError } from "@/lib/harness-api";
import { HOTEL_A1, HOTEL_B1, ORG_A, USER_ID } from "@/test/fixtures";

const TENANT = { orgId: ORG_A, hotelId: HOTEL_A1 };
const ME_BODY = { user_id: USER_ID, org_id: ORG_A, hotel_id: HOTEL_A1, role: "operator" };

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiWith(response: Response | Error | DOMException) {
  const fetchImpl = vi.fn<typeof fetch>(async () => {
    if (!(response instanceof Response)) throw response;
    return response;
  });
  return { api: createHarnessApi({ baseUrl: "https://api.example.test/", fetchImpl }), fetchImpl };
}

describe("HarnessApi.getMe", () => {
  it("names the tenant in the path and sends the user's JWT as a Bearer token", async () => {
    const { api, fetchImpl } = apiWith(jsonResponse(200, ME_BODY));
    await api.getMe(TENANT, "jwt-abc");

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe(`https://api.example.test/v1/orgs/${ORG_A}/hotels/${HOTEL_A1}/me`);
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer jwt-abc");
    // No cookies and no tenant ids outside the path (docs/auth.md: path names the tenant).
    expect(init?.credentials).toBe("omit");
    expect(init?.body).toBeUndefined();
    expect(String(url)).not.toContain("?");
  });

  it("returns the caller's role in that hotel", async () => {
    const { api } = apiWith(jsonResponse(200, ME_BODY));
    expect(await api.getMe(TENANT, "jwt")).toEqual({
      userId: USER_ID,
      orgId: ORG_A,
      hotelId: HOTEL_A1,
      role: "operator",
    });
  });

  it.each([
    [401, "unauthenticated"],
    [403, "forbidden"],
    [404, "unexpected"],
    [422, "invalid_request"],
    [500, "unexpected"],
    [503, "unavailable"],
  ])("maps HTTP %i to %s", async (status, kind) => {
    const { api } = apiWith(jsonResponse(status, { detail: "x" }));
    await expect(api.getMe(TENANT, "jwt")).rejects.toMatchObject({ kind, status });
  });

  it("maps a network failure to network", async () => {
    const { api } = apiWith(new TypeError("Failed to fetch"));
    await expect(api.getMe(TENANT, "jwt")).rejects.toMatchObject({ kind: "network" });
  });

  it("passes an abort through as an AbortError, not as an API error", async () => {
    const { api } = apiWith(new DOMException("aborted", "AbortError"));
    const error = await api.getMe(TENANT, "jwt").catch((e: unknown) => e);
    expect(error).not.toBeInstanceOf(HarnessApiError);
    expect((error as DOMException).name).toBe("AbortError");
  });

  it.each([
    ["another hotel", { ...ME_BODY, hotel_id: HOTEL_B1 }],
    ["an unknown role", { ...ME_BODY, role: "superuser" }],
    ["a missing field", { org_id: ORG_A, hotel_id: HOTEL_A1, role: "viewer" }],
  ])("refuses a 200 body that names %s", async (_label, body) => {
    const { api } = apiWith(jsonResponse(200, body));
    await expect(api.getMe(TENANT, "jwt")).rejects.toMatchObject({ kind: "unexpected" });
  });

  it("refuses ids that are not UUIDs before any request", async () => {
    const { api, fetchImpl } = apiWith(jsonResponse(200, ME_BODY));
    await expect(api.getMe({ orgId: "../admin", hotelId: HOTEL_A1 }, "jwt")).rejects.toMatchObject({
      kind: "invalid_request",
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("refuses an empty token before any request", async () => {
    const { api, fetchImpl } = apiWith(jsonResponse(200, ME_BODY));
    await expect(api.getMe(TENANT, "")).rejects.toMatchObject({ kind: "unauthenticated" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
