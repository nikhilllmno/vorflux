// Reads the signed-in user's hotels from the Supabase Data API with the user's own JWT, so the
// PR #5 RLS policies apply. The rows are parsed here and the membership scope rule runs again in
// `buildHotelAccessList`, so the switcher never lists a hotel the user's memberships do not cover.
import {
  buildHotelAccessList,
  isHotelRole,
  isUuid,
  type HotelAccess,
  type HotelRow,
  type MembershipRow,
} from "@/lib/tenancy";

export interface HotelDirectory {
  listMyHotels(userId: string): Promise<HotelAccess[]>;
}

/** The UI shows a fixed message; database details stay out of it. */
export class HotelDirectoryError extends Error {
  constructor() {
    super("Could not load the hotel list.");
    this.name = "HotelDirectoryError";
  }
}

export interface QueryResult {
  data: unknown;
  error: { message: string } | null;
}

export interface FilterBuilder extends PromiseLike<QueryResult> {
  eq(column: string, value: string): FilterBuilder;
  in(column: string, values: readonly string[]): FilterBuilder;
  order(column: string): FilterBuilder;
  range(from: number, to: number): FilterBuilder;
}

/** The part of the Supabase client this module uses. */
export interface TableReader {
  from(table: string): { select(columns: string): FilterBuilder };
}

type Row = Record<string, unknown>;

function rows(result: QueryResult): Row[] {
  if (result.error || !Array.isArray(result.data)) throw new HotelDirectoryError();
  return result.data.filter((r): r is Row => typeof r === "object" && r !== null);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function parseMembership(row: Row, userId: string): MembershipRow | null {
  const { user_id, org_id, hotel_id, role } = row;
  if (user_id !== userId || !isUuid(org_id) || !isHotelRole(role)) return null;
  if (hotel_id !== null && !isUuid(hotel_id)) return null;
  return { orgId: org_id.toLowerCase(), hotelId: hotel_id?.toLowerCase() ?? null, role };
}

function parseHotel(row: Row): HotelRow | null {
  const org = row.orgs;
  if (typeof org !== "object" || org === null || Array.isArray(org)) return null;
  const { id: orgId, slug: orgSlug, name: orgName } = org as Row;
  const { id, org_id, slug, name } = row;
  if (!isUuid(id) || !isUuid(org_id) || !isUuid(orgId)) return null;
  if (orgId.toLowerCase() !== org_id.toLowerCase()) return null;
  const hotelSlug = text(slug);
  const hotelName = text(name);
  const oSlug = text(orgSlug);
  const oName = text(orgName);
  if (!hotelSlug || !hotelName || !oSlug || !oName) return null;
  return {
    id: id.toLowerCase(),
    orgId: org_id.toLowerCase(),
    slug: hotelSlug,
    name: hotelName,
    orgSlug: oSlug,
    orgName: oName,
  };
}

// PostgREST returns at most `max_rows` rows per request (1000 by default) without an error, so
// every read pages until an empty page. The cap bounds a misbehaving server.
const PAGE_SIZE = 1000;
const MAX_PAGES = 20;

/** Reads every row, ordered by `id` so the pages are stable. */
async function readAll(makeQuery: () => FilterBuilder): Promise<Row[]> {
  const all: Row[] = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    let batch: Row[];
    try {
      batch = rows(
        await makeQuery()
          .order("id")
          .range(all.length, all.length + PAGE_SIZE - 1),
      );
    } catch {
      throw new HotelDirectoryError();
    }
    if (batch.length === 0) return all;
    all.push(...batch);
  }
  throw new HotelDirectoryError();
}

export function createSupabaseHotelDirectory(client: TableReader): HotelDirectory {
  return {
    async listMyHotels(userId) {
      if (!isUuid(userId)) throw new HotelDirectoryError();
      const id = userId.toLowerCase();

      // RLS shows admins other users' rows too, so filter on the caller's own user id.
      const memberships = (
        await readAll(() =>
          client.from("memberships").select("user_id, org_id, hotel_id, role").eq("user_id", id),
        )
      )
        .map((row) => parseMembership(row, id))
        .filter((m): m is MembershipRow => m !== null);
      if (memberships.length === 0) return [];

      const orgIds = [...new Set(memberships.map((m) => m.orgId))];
      const hotels = (
        await readAll(() =>
          client
            .from("hotels")
            .select("id, org_id, slug, name, orgs(id, slug, name)")
            .in("org_id", orgIds),
        )
      )
        .map(parseHotel)
        .filter((h): h is HotelRow => h !== null);

      return buildHotelAccessList(memberships, hotels);
    },
  };
}
