// Shared test data and in-memory fakes. Ids are random-looking UUIDs, not real tenants.
import { vi } from "vitest";

import type { AuthGateway, SignInResult, SignedInUser } from "@/lib/auth";
import type { HarnessApi, MeResponse } from "@/lib/harness-api";
import type { HotelDirectory } from "@/lib/hotel-directory";
import { createSelectionStore } from "@/lib/selection-store";
import type { HotelAccess, TenantRef } from "@/lib/tenancy";

export const USER_ID = "6f1c2b9e-1d2a-4c3b-8e4f-5a6b7c8d9e01";
export const OTHER_USER_ID = "0b7e3c1a-9f8d-4e6c-a5b4-3c2d1e0f9a8b";

export const ORG_A = "a0000000-0000-4000-8000-00000000000a";
export const ORG_B = "b0000000-0000-4000-8000-00000000000b";
export const HOTEL_A1 = "a1000000-0000-4000-8000-0000000000a1";
export const HOTEL_A2 = "a2000000-0000-4000-8000-0000000000a2";
export const HOTEL_B1 = "b1000000-0000-4000-8000-0000000000b1";

export const ACCESS_A1: HotelAccess = {
  orgId: ORG_A,
  orgName: "Acme Hotels",
  orgSlug: "acme",
  hotelId: HOTEL_A1,
  hotelName: "Acme Beach",
  hotelSlug: "acme-beach",
  role: "operator",
};
export const ACCESS_A2: HotelAccess = {
  orgId: ORG_A,
  orgName: "Acme Hotels",
  orgSlug: "acme",
  hotelId: HOTEL_A2,
  hotelName: "Acme City",
  hotelSlug: "acme-city",
  role: "admin",
};
export const ACCESS_B1: HotelAccess = {
  orgId: ORG_B,
  orgName: "Bloom Stays",
  orgSlug: "bloom",
  hotelId: HOTEL_B1,
  hotelName: "Bloom Ranch",
  hotelSlug: "bloom-ranch",
  role: "viewer",
};

export const USER: SignedInUser = { id: USER_ID, email: "staff@example.test" };

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** An AuthGateway whose state the test drives. */
export function fakeAuth(initial: SignedInUser | null = null) {
  let user = initial;
  let token = initial ? "token-1" : null;
  const listeners = new Set<(u: SignedInUser | null) => void>();
  const emit = (next: SignedInUser | null, nextToken: string | null = next ? "token-1" : null) => {
    user = next;
    token = nextToken;
    listeners.forEach((listener) => listener(next));
  };
  const auth = {
    currentUser: vi.fn(async () => user),
    onUserChange: vi.fn((listener: (u: SignedInUser | null) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }),
    signIn: vi.fn(async (): Promise<SignInResult> => {
      emit(USER);
      return { ok: true };
    }),
    signOut: vi.fn(async () => emit(null)),
    accessToken: vi.fn(async () => token),
  } satisfies AuthGateway;
  return {
    auth,
    emit,
    setToken: (t: string | null) => (token = t),
    listenerCount: () => listeners.size,
  };
}

export function fakeDirectory(hotels: HotelAccess[] = [ACCESS_A1, ACCESS_A2, ACCESS_B1]) {
  return {
    listMyHotels: vi.fn(async (userId: string) => {
      void userId;
      return hotels;
    }),
  } satisfies HotelDirectory;
}

export function meFor(tenant: TenantRef, role: MeResponse["role"] = "operator"): MeResponse {
  return { userId: USER_ID, orgId: tenant.orgId, hotelId: tenant.hotelId, role };
}

export function fakeApi() {
  return {
    getMe: vi.fn(async (tenant: TenantRef, token: string, signal?: AbortSignal) => {
      void token;
      void signal;
      return meFor(tenant);
    }),
  } satisfies HarnessApi;
}

export function fakeDeps(options: { user?: SignedInUser | null; hotels?: HotelAccess[] } = {}) {
  const auth = fakeAuth(options.user === undefined ? USER : options.user);
  return {
    authControl: auth,
    deps: {
      auth: auth.auth,
      hotels: fakeDirectory(options.hotels),
      api: fakeApi(),
      selection: createSelectionStore(window.localStorage),
    },
  };
}
