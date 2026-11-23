import { describe, expect, it } from "vitest";

import {
  createSupabaseHotelDirectory,
  HotelDirectoryError,
  type QueryResult,
  type TableReader,
} from "@/lib/hotel-directory";
import {
  HOTEL_A1,
  HOTEL_A2,
  HOTEL_B1,
  ORG_A,
  ORG_B,
  OTHER_USER_ID,
  USER_ID,
} from "@/test/fixtures";

type Call = { table: string; columns: string; filters: Array<[string, string, unknown]> };

/** A stand-in for the Supabase Data API: records each query and answers per table. */
function fakeReader(answers: Record<string, QueryResult>) {
  const calls: Call[] = [];
  const reader: TableReader = {
    from(table) {
      return {
        select(columns) {
          const call: Call = { table, columns, filters: [] };
          calls.push(call);
          const builder = {
            eq(column: string, value: string) {
              call.filters.push(["eq", column, value]);
              return builder;
            },
            in(column: string, values: readonly string[]) {
              call.filters.push(["in", column, [...values]]);
              return builder;
            },
            then<R1 = QueryResult, R2 = never>(
              onFulfilled?: ((value: QueryResult) => R1 | PromiseLike<R1>) | null,
              onRejected?: ((reason: unknown) => R2 | PromiseLike<R2>) | null,
            ) {
              const answer = answers[table] ?? { data: [], error: null };
              return Promise.resolve(answer).then(onFulfilled, onRejected);
            },
          };
          return builder;
        },
      };
    },
  };
  return { reader, calls };
}

function m(orgId: string, hotelId: string | null, role: string, userId = USER_ID) {
  return { user_id: userId, org_id: orgId, hotel_id: hotelId, role };
}

const hotelRows = [
  { id: HOTEL_A1, org_id: ORG_A, slug: "acme-beach", name: "Acme Beach", orgs: { id: ORG_A, slug: "acme", name: "Acme Hotels" } },
  { id: HOTEL_A2, org_id: ORG_A, slug: "acme-city", name: "Acme City", orgs: { id: ORG_A, slug: "acme", name: "Acme Hotels" } },
  { id: HOTEL_B1, org_id: ORG_B, slug: "bloom-ranch", name: "Bloom Ranch", orgs: { id: ORG_B, slug: "bloom", name: "Bloom Stays" } },
];

describe("createSupabaseHotelDirectory", () => {
  it("reads only the signed-in user's membership rows", async () => {
    // RLS lets an admin read other users' memberships, so the query must filter on user_id.
    const { reader, calls } = fakeReader({
      memberships: { data: [m(ORG_A, HOTEL_A1, "operator")], error: null },
      hotels: { data: hotelRows, error: null },
    });
    await createSupabaseHotelDirectory(reader).listMyHotels(USER_ID);

    const memberships = calls.find((c) => c.table === "memberships");
    expect(memberships?.filters).toContainEqual(["eq", "user_id", USER_ID]);
  });

  it("asks for hotels only in the user's orgs and lists only covered hotels", async () => {
    const { reader, calls } = fakeReader({
      memberships: { data: [m(ORG_A, HOTEL_A1, "operator")], error: null },
      hotels: { data: hotelRows, error: null },
    });
    const list = await createSupabaseHotelDirectory(reader).listMyHotels(USER_ID);

    expect(list.map((h) => [h.orgId, h.hotelId, h.role, h.orgName, h.hotelName])).toEqual([
      [ORG_A, HOTEL_A1, "operator", "Acme Hotels", "Acme Beach"],
    ]);
    const hotels = calls.find((c) => c.table === "hotels");
    expect(hotels?.filters).toContainEqual(["in", "org_id", [ORG_A]]);
  });

  it("does not query hotels when the user has no memberships", async () => {
    const { reader, calls } = fakeReader({ memberships: { data: [], error: null } });
    expect(await createSupabaseHotelDirectory(reader).listMyHotels(USER_ID)).toEqual([]);
    expect(calls.map((c) => c.table)).toEqual(["memberships"]);
  });

  it("drops membership rows of another user, unknown roles and malformed ids", async () => {
    const { reader } = fakeReader({
      memberships: {
        data: [
          m(ORG_A, HOTEL_A1, "superuser"),
          m("not-a-uuid", null, "owner"),
          m(ORG_B, null, "viewer", OTHER_USER_ID),
          { org_id: ORG_A, hotel_id: null, role: "owner" },
          m(ORG_A, HOTEL_A2, "viewer"),
        ],
        error: null,
      },
      hotels: { data: hotelRows, error: null },
    });
    const list = await createSupabaseHotelDirectory(reader).listMyHotels(USER_ID);
    expect(list.map((h) => [h.hotelId, h.role])).toEqual([[HOTEL_A2, "viewer"]]);
  });

  it("drops hotel rows whose embedded org does not match their org_id", async () => {
    const { reader } = fakeReader({
      memberships: { data: [m(ORG_A, null, "viewer")], error: null },
      hotels: {
        data: [
          { ...hotelRows[0], orgs: { id: ORG_B, slug: "bloom", name: "Bloom Stays" } },
          hotelRows[1],
        ],
        error: null,
      },
    });
    const list = await createSupabaseHotelDirectory(reader).listMyHotels(USER_ID);
    expect(list.map((h) => h.hotelId)).toEqual([HOTEL_A2]);
  });

  it("refuses a user id that is not a UUID without querying", async () => {
    const { reader, calls } = fakeReader({});
    await expect(createSupabaseHotelDirectory(reader).listMyHotels("anything")).rejects.toBeInstanceOf(
      HotelDirectoryError,
    );
    expect(calls).toEqual([]);
  });

  it.each(["memberships", "hotels"])("turns a %s query error into HotelDirectoryError", async (table) => {
    const { reader } = fakeReader({
      memberships: { data: [m(ORG_A, null, "viewer")], error: null },
      [table]: { data: null, error: { message: "permission denied for table secret_detail" } },
    });
    const error = await createSupabaseHotelDirectory(reader)
      .listMyHotels(USER_ID)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(HotelDirectoryError);
    // The database message stays out of the UI text.
    expect(String((error as Error).message)).not.toContain("secret_detail");
  });
});
