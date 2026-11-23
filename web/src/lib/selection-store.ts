// Remembers the last hotel each user picked, in browser storage. It stores ids only; the hotel
// list from the directory decides whether the remembered hotel is still allowed.
import { isUuid, type TenantRef } from "@/lib/tenancy";

export interface SelectionStore {
  load(userId: string): TenantRef | null;
  save(userId: string, tenant: TenantRef): void;
  clear(userId: string): void;
}

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

const PREFIX = "harness.selectedHotel.v1:";

export function createSelectionStore(storage: StorageLike | null): SelectionStore {
  // Storage can throw (blocked cookies, private mode, full quota). The choice is a convenience,
  // so every failure falls back to "nothing remembered".
  const attempt = <T>(fn: (s: StorageLike) => T, fallback: T): T => {
    if (!storage) return fallback;
    try {
      return fn(storage);
    } catch {
      return fallback;
    }
  };

  return {
    load(userId) {
      return attempt((s) => {
        const raw = s.getItem(PREFIX + userId);
        if (raw === null) return null;
        const value: unknown = JSON.parse(raw);
        if (typeof value !== "object" || value === null) return null;
        const { orgId, hotelId } = value as Record<string, unknown>;
        return isUuid(orgId) && isUuid(hotelId) ? { orgId, hotelId } : null;
      }, null);
    },
    save(userId, tenant) {
      attempt(
        (s) =>
          s.setItem(
            PREFIX + userId,
            JSON.stringify({ orgId: tenant.orgId, hotelId: tenant.hotelId }),
          ),
        undefined,
      );
    },
    clear(userId) {
      attempt((s) => s.removeItem(PREFIX + userId), undefined);
    },
  };
}
