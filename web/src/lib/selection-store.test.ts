import { describe, expect, it } from "vitest";

import { createSelectionStore } from "@/lib/selection-store";
import { HOTEL_A1, ORG_A, OTHER_USER_ID, USER_ID } from "@/test/fixtures";

const TENANT = { orgId: ORG_A, hotelId: HOTEL_A1 };

describe("createSelectionStore", () => {
  it("remembers the last hotel per user", () => {
    const store = createSelectionStore(window.localStorage);
    store.save(USER_ID, TENANT);
    expect(store.load(USER_ID)).toEqual(TENANT);
    expect(store.load(OTHER_USER_ID)).toBeNull();
  });

  it("forgets the hotel on clear", () => {
    const store = createSelectionStore(window.localStorage);
    store.save(USER_ID, TENANT);
    store.clear(USER_ID);
    expect(store.load(USER_ID)).toBeNull();
  });

  it("stores ids only", () => {
    createSelectionStore(window.localStorage).save(USER_ID, TENANT);
    const values = Object.keys(window.localStorage).map((k) => window.localStorage.getItem(k));
    expect(values).toEqual([JSON.stringify(TENANT)]);
  });

  it.each(["not json", JSON.stringify({ orgId: "x", hotelId: HOTEL_A1 }), JSON.stringify([1])])(
    "ignores a damaged entry %s",
    (raw) => {
      const store = createSelectionStore(window.localStorage);
      store.save(USER_ID, TENANT);
      const key = Object.keys(window.localStorage)[0];
      window.localStorage.setItem(key, raw);
      expect(store.load(USER_ID)).toBeNull();
    },
  );

  it("works without storage (for example when the browser blocks it)", () => {
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    };
    for (const storage of [null, throwing]) {
      const store = createSelectionStore(storage);
      expect(() => store.save(USER_ID, TENANT)).not.toThrow();
      expect(store.load(USER_ID)).toBeNull();
      expect(() => store.clear(USER_ID)).not.toThrow();
    }
  });
});
