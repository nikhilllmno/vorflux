// Client for the control-plane API. Follows docs/auth.md (PR #9): the path names the tenant
// (`/v1/orgs/{org_id}/hotels/{hotel_id}/...`), the user's Supabase access token goes in
// `Authorization: Bearer`, and every denial for a valid token is one identical 403.
import { isHotelRole, isUuid, sameTenant, type HotelRole, type TenantRef } from "@/lib/tenancy";

export type HarnessApiErrorKind =
  | "unauthenticated" // 401: no token, or the API refused it
  | "forbidden" // 403: no access to this tenant (same answer for every cause)
  | "invalid_request" // 422, or ids refused before sending
  | "unavailable" // 503: auth or membership data not available on the server
  | "network" // no answer
  | "unexpected"; // any other status, or a body that does not match the contract

export class HarnessApiError extends Error {
  constructor(
    readonly kind: HarnessApiErrorKind,
    readonly status?: number,
  ) {
    super(status === undefined ? `Harness API: ${kind}` : `Harness API: ${kind} (HTTP ${status})`);
    this.name = "HarnessApiError";
  }
}

export interface MeResponse extends TenantRef {
  userId: string;
  role: HotelRole;
}

export interface HarnessApi {
  /** `GET /v1/orgs/{org_id}/hotels/{hotel_id}/me`: the caller's role in that hotel. */
  getMe(tenant: TenantRef, accessToken: string, signal?: AbortSignal): Promise<MeResponse>;
}

const STATUS_KIND: Record<number, HarnessApiErrorKind> = {
  401: "unauthenticated",
  403: "forbidden",
  422: "invalid_request",
  503: "unavailable",
};

function tenantPath(tenant: TenantRef, rest: string): string {
  if (!isUuid(tenant.orgId) || !isUuid(tenant.hotelId)) {
    throw new HarnessApiError("invalid_request");
  }
  return `/v1/orgs/${tenant.orgId.toLowerCase()}/hotels/${tenant.hotelId.toLowerCase()}/${rest}`;
}

function parseMe(body: unknown, tenant: TenantRef): MeResponse {
  if (typeof body === "object" && body !== null) {
    const { user_id, org_id, hotel_id, role } = body as Record<string, unknown>;
    if (isUuid(user_id) && isUuid(org_id) && isUuid(hotel_id) && isHotelRole(role)) {
      const me = {
        userId: user_id,
        orgId: org_id.toLowerCase(),
        hotelId: hotel_id.toLowerCase(),
        role,
      };
      if (
        sameTenant(me, { orgId: tenant.orgId.toLowerCase(), hotelId: tenant.hotelId.toLowerCase() })
      ) {
        return me;
      }
    }
  }
  throw new HarnessApiError("unexpected", 200);
}

export function createHarnessApi(options: {
  baseUrl: string;
  fetchImpl?: typeof fetch;
}): HarnessApi {
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));

  async function getJson(path: string, accessToken: string, signal?: AbortSignal) {
    if (!accessToken) throw new HarnessApiError("unauthenticated");
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method: "GET",
        headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
        credentials: "omit",
        cache: "no-store",
        signal,
      });
    } catch (error) {
      // A cancelled request is not an API failure; the caller ignores it.
      if (signal?.aborted || (error as { name?: unknown } | null)?.name === "AbortError")
        throw error;
      throw new HarnessApiError("network");
    }
    if (!response.ok) {
      throw new HarnessApiError(STATUS_KIND[response.status] ?? "unexpected", response.status);
    }
    try {
      return (await response.json()) as unknown;
    } catch {
      throw new HarnessApiError("unexpected", response.status);
    }
  }

  return {
    async getMe(tenant, accessToken, signal) {
      const path = tenantPath(tenant, "me");
      return parseMe(await getJson(path, accessToken, signal), tenant);
    },
  };
}
