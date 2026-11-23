import { describe, expect, it } from "vitest";

import {
  buildHotelAccessList,
  chooseHotel,
  groupByOrg,
  isUuid,
  type HotelRow,
  type MembershipRow,
} from "@/lib/tenancy";
import {
  ACCESS_A1,
  ACCESS_A2,
  ACCESS_B1,
  HOTEL_A1,
  HOTEL_A2,
  HOTEL_B1,
  ORG_A,
  ORG_B,
} from "@/test/fixtures";

const hotelA1: HotelRow = {
  id: HOTEL_A1,
  orgId: ORG_A,
  slug: "acme-beach",
  name: "Acme Beach",
  orgSlug: "acme",
  orgName: "Acme Hotels",
};
const hotelA2: HotelRow = {
  id: HOTEL_A2,
  orgId: ORG_A,
  slug: "acme-city",
  name: "Acme City",
  orgSlug: "acme",
  orgName: "Acme Hotels",
};
const hotelB1: HotelRow = {
  id: HOTEL_B1,
  orgId: ORG_B,
  slug: "bloom-ranch",
  name: "Bloom Ranch",
  orgSlug: "bloom",
  orgName: "Bloom Stays",
};

describe("buildHotelAccessList (the switcher lists only the user's memberships)", () => {
  it("lists a hotel only when one of the user's memberships covers it", () => {
    const memberships: MembershipRow[] = [{ orgId: ORG_A, hotelId: HOTEL_A1, role: "operator" }];
    const list = buildHotelAccessList(memberships, [hotelA1, hotelA2, hotelB1]);
    expect(list.map((h) => h.hotelId)).toEqual([HOTEL_A1]);
  });

  it("an org-wide membership covers every hotel of that org and no other org", () => {
    const memberships: MembershipRow[] = [{ orgId: ORG_A, hotelId: null, role: "viewer" }];
    const list = buildHotelAccessList(memberships, [hotelA1, hotelA2, hotelB1]);
    expect(list.map((h) => h.hotelId).sort()).toEqual([HOTEL_A1, HOTEL_A2].sort());
  });

  it("does not list a hotel that a membership names under another org", () => {
    // A membership row that pairs org B with a hotel of org A must not reach that hotel.
    const memberships: MembershipRow[] = [{ orgId: ORG_B, hotelId: HOTEL_A1, role: "admin" }];
    expect(buildHotelAccessList(memberships, [hotelA1, hotelB1])).toEqual([]);
  });

  it("uses the highest covering role, and admin in one hotel does not carry to another", () => {
    const memberships: MembershipRow[] = [
      { orgId: ORG_A, hotelId: null, role: "viewer" },
      { orgId: ORG_A, hotelId: HOTEL_A2, role: "admin" },
    ];
    const list = buildHotelAccessList(memberships, [hotelA1, hotelA2]);
    expect(Object.fromEntries(list.map((h) => [h.hotelId, h.role]))).toEqual({
      [HOTEL_A1]: "viewer",
      [HOTEL_A2]: "admin",
    });
  });

  it("returns an empty list when the user has no memberships", () => {
    expect(buildHotelAccessList([], [hotelA1, hotelB1])).toEqual([]);
  });

  it("sorts by organisation name, then hotel name", () => {
    const memberships: MembershipRow[] = [
      { orgId: ORG_A, hotelId: null, role: "operator" },
      { orgId: ORG_B, hotelId: null, role: "operator" },
    ];
    const list = buildHotelAccessList(memberships, [hotelB1, hotelA1, hotelA2]);
    expect(list.map((h) => h.hotelName)).toEqual(["Acme Beach", "Acme City", "Bloom Ranch"]);
  });
});

describe("chooseHotel", () => {
  const list = [ACCESS_A1, ACCESS_A2, ACCESS_B1];

  it("keeps the preferred hotel when the user still has it", () => {
    expect(chooseHotel(list, { orgId: ORG_A, hotelId: HOTEL_A2 })).toBe(ACCESS_A2);
  });

  it("falls back to the first hotel when the preferred one is gone", () => {
    const gone = { orgId: ORG_B, hotelId: "c0000000-0000-4000-8000-0000000000c1" };
    expect(chooseHotel(list, gone)).toBe(ACCESS_A1);
  });

  it("needs both ids to match: a known hotel id under another org is not kept", () => {
    expect(chooseHotel(list, { orgId: ORG_B, hotelId: HOTEL_A2 })).toBe(ACCESS_A1);
  });

  it("returns null for an empty list", () => {
    expect(chooseHotel([], { orgId: ORG_A, hotelId: HOTEL_A1 })).toBeNull();
  });
});

describe("groupByOrg", () => {
  it("groups hotels under their organisation in list order", () => {
    expect(groupByOrg([ACCESS_A1, ACCESS_A2, ACCESS_B1])).toEqual([
      { orgId: ORG_A, orgName: "Acme Hotels", hotels: [ACCESS_A1, ACCESS_A2] },
      { orgId: ORG_B, orgName: "Bloom Stays", hotels: [ACCESS_B1] },
    ]);
  });
});

describe("isUuid", () => {
  it.each([HOTEL_A1, HOTEL_A1.toUpperCase()])("accepts %s", (value) => {
    expect(isUuid(value)).toBe(true);
  });

  it.each(["", "not-a-uuid", `${HOTEL_A1}/x`, "../../etc", 42, null])("refuses %s", (value) => {
    expect(isUuid(value)).toBe(false);
  });
});
