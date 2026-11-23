// Tenant model for the web UI. Mirrors backend/src/harness/tenancy.py and docs/auth.md (PR #9):
// roles rank viewer < operator < admin < owner, and the effective role in a hotel is the highest
// role of the user's memberships that cover it (an org-wide row or a row for that hotel).

export const HOTEL_ROLES = ["viewer", "operator", "admin", "owner"] as const;
export type HotelRole = (typeof HOTEL_ROLES)[number];

export function isHotelRole(value: unknown): value is HotelRole {
  return typeof value === "string" && (HOTEL_ROLES as readonly string[]).includes(value);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

/** The tenant a request names: `/v1/orgs/{orgId}/hotels/{hotelId}/...`. */
export interface TenantRef {
  orgId: string;
  hotelId: string;
}

export interface MembershipRow {
  orgId: string;
  /** null means the membership covers every hotel of the org. */
  hotelId: string | null;
  role: HotelRole;
}

export interface HotelRow {
  id: string;
  orgId: string;
  slug: string;
  name: string;
  orgSlug: string;
  orgName: string;
}

export interface HotelAccess extends TenantRef {
  orgName: string;
  orgSlug: string;
  hotelName: string;
  hotelSlug: string;
  role: HotelRole;
}

function rank(role: HotelRole): number {
  return HOTEL_ROLES.indexOf(role);
}

/**
 * The hotels the user may pick: each hotel that one of the user's memberships covers, with the
 * highest covering role. A hotel no membership covers is never listed, even if the caller passes
 * it, so the switcher does not depend on RLS alone.
 */
export function buildHotelAccessList(
  memberships: readonly MembershipRow[],
  hotels: readonly HotelRow[],
): HotelAccess[] {
  const list: HotelAccess[] = [];
  for (const hotel of hotels) {
    let role: HotelRole | null = null;
    for (const m of memberships) {
      const covers = m.orgId === hotel.orgId && (m.hotelId === null || m.hotelId === hotel.id);
      if (covers && (role === null || rank(m.role) > rank(role))) role = m.role;
    }
    if (role === null) continue;
    list.push({
      orgId: hotel.orgId,
      orgName: hotel.orgName,
      orgSlug: hotel.orgSlug,
      hotelId: hotel.id,
      hotelName: hotel.name,
      hotelSlug: hotel.slug,
      role,
    });
  }
  return list.sort(
    (a, b) =>
      a.orgName.localeCompare(b.orgName) ||
      a.orgId.localeCompare(b.orgId) ||
      a.hotelName.localeCompare(b.hotelName) ||
      a.hotelId.localeCompare(b.hotelId),
  );
}

export function sameTenant(a: TenantRef, b: TenantRef): boolean {
  return a.orgId === b.orgId && a.hotelId === b.hotelId;
}

/** The preferred hotel if the user still has it, else the first hotel, else null. */
export function chooseHotel(
  list: readonly HotelAccess[],
  preferred: TenantRef | null,
): HotelAccess | null {
  if (preferred) {
    const match = list.find((h) => sameTenant(h, preferred));
    if (match) return match;
  }
  return list[0] ?? null;
}

export interface OrgGroup {
  orgId: string;
  orgName: string;
  hotels: HotelAccess[];
}

export function groupByOrg(list: readonly HotelAccess[]): OrgGroup[] {
  const groups: OrgGroup[] = [];
  for (const hotel of list) {
    let group = groups.find((g) => g.orgId === hotel.orgId);
    if (!group) {
      group = { orgId: hotel.orgId, orgName: hotel.orgName, hotels: [] };
      groups.push(group);
    }
    group.hotels.push(hotel);
  }
  return groups;
}
