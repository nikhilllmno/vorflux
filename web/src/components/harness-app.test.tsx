import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { HarnessApp } from "@/components/harness-app";
import { HarnessApiError, type MeResponse } from "@/lib/harness-api";
import { HotelDirectoryError } from "@/lib/hotel-directory";
import {
  ACCESS_A1,
  ACCESS_B1,
  deferred,
  fakeDeps,
  HOTEL_A1,
  HOTEL_A2,
  HOTEL_B1,
  meFor,
  ORG_A,
  ORG_B,
  USER,
  USER_ID,
} from "@/test/fixtures";

async function signIn(email = "staff@example.test", password = "correct horse") {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

describe("sign-in and sign-out", () => {
  it("shows the sign-in form when there is no session, with no sign-up path", async () => {
    const { deps } = fakeDeps({ user: null });
    render(<HarnessApp deps={deps} />);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeDefined();
    expect(screen.queryByText(/sign up|create account/i)).toBeNull();
    expect(deps.hotels.listMyHotels).not.toHaveBeenCalled();
  });

  it("signs in with Supabase Auth and then lists the user's hotels", async () => {
    const { deps } = fakeDeps({ user: null });
    render(<HarnessApp deps={deps} />);
    await signIn("staff@example.test", "correct horse");

    expect(deps.auth.signIn).toHaveBeenCalledWith("staff@example.test", "correct horse");
    expect(await screen.findByRole("combobox", { name: "Hotel" })).toBeDefined();
    expect(deps.hotels.listMyHotels).toHaveBeenCalledWith(USER_ID);
    expect(screen.getByText("staff@example.test")).toBeDefined();
  });

  it.each([
    ["invalid_credentials", "The email or password is not correct."],
    ["rate_limited", "Too many sign-in attempts. Wait a minute and try again."],
    ["unavailable", "Sign-in is not available now. Try again later."],
  ] as const)("shows a plain message for %s and keeps the form usable", async (reason, message) => {
    const { deps } = fakeDeps({ user: null });
    deps.auth.signIn.mockResolvedValueOnce({ ok: false, reason });
    render(<HarnessApp deps={deps} />);
    await signIn();

    expect((await screen.findByRole("alert")).textContent).toBe(message);
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Email") as HTMLInputElement).value).toBe("staff@example.test");
    expect((screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("disables the form while a sign-in is in flight, so one click sends one request", async () => {
    const { deps } = fakeDeps({ user: null });
    const pending = deferred<{ ok: false; reason: "invalid_credentials" }>();
    deps.auth.signIn.mockReturnValueOnce(pending.promise);
    render(<HarnessApp deps={deps} />);
    const user = await signIn();

    const button = screen.getByRole("button", { name: "Signing in…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await user.click(button);
    expect(deps.auth.signIn).toHaveBeenCalledTimes(1);

    await act(async () => pending.resolve({ ok: false, reason: "invalid_credentials" }));
    expect((screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("signs out, forgets the chosen hotel and returns to the sign-in form", async () => {
    const { deps } = fakeDeps();
    render(<HarnessApp deps={deps} />);
    await screen.findByText(/as operator/);
    expect(deps.selection.load(USER_ID)).not.toBeNull();

    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));

    expect(deps.auth.signOut).toHaveBeenCalledOnce();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeDefined();
    expect(deps.selection.load(USER_ID)).toBeNull();
  });

  it("returns to the sign-in form when Supabase ends the session", async () => {
    const { deps, authControl } = fakeDeps();
    render(<HarnessApp deps={deps} />);
    await screen.findByRole("combobox", { name: "Hotel" });

    act(() => authControl.emit(null));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeDefined();
  });

  it("stops listening to auth events when it unmounts", async () => {
    const { deps, authControl } = fakeDeps();
    const { unmount } = render(<HarnessApp deps={deps} />);
    await screen.findByRole("combobox", { name: "Hotel" });
    unmount();
    expect(authControl.listenerCount()).toBe(0);
  });
});

describe("hotel switcher", () => {
  it("lists only the hotels the directory returns for this user, grouped by organization", async () => {
    const { deps } = fakeDeps({ hotels: [ACCESS_A1, ACCESS_B1] });
    render(<HarnessApp deps={deps} />);
    const select = await screen.findByRole("combobox", { name: "Hotel" });

    const groups = within(select).getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("label"))).toEqual(["Acme Hotels", "Bloom Stays"]);
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Acme Beach", "Bloom Ranch"]);
    expect(screen.queryByText("Acme City")).toBeNull();
  });

  it("checks the first hotel with the API, sending the user's current JWT", async () => {
    const { deps, authControl } = fakeDeps();
    authControl.setToken("fresh-token");
    render(<HarnessApp deps={deps} />);

    expect(
      await screen.findByText("You work in Acme Beach (Acme Hotels) as operator."),
    ).toBeDefined();
    expect(deps.api.getMe).toHaveBeenCalledWith(
      { orgId: ORG_A, hotelId: HOTEL_A1 },
      "fresh-token",
      expect.any(AbortSignal),
    );
  });

  it("switches hotel, calls the API for the new hotel and remembers the choice", async () => {
    const { deps } = fakeDeps();
    deps.api.getMe.mockImplementation(async (tenant) =>
      meFor(tenant, tenant.hotelId === HOTEL_B1 ? "viewer" : "operator"),
    );
    render(<HarnessApp deps={deps} />);
    const select = await screen.findByRole("combobox", { name: "Hotel" });
    await screen.findByText(/Acme Beach/, { selector: "p" });

    await userEvent.setup().selectOptions(select, HOTEL_B1);

    expect(
      await screen.findByText("You work in Bloom Ranch (Bloom Stays) as viewer."),
    ).toBeDefined();
    expect(deps.api.getMe).toHaveBeenLastCalledWith(
      { orgId: ORG_B, hotelId: HOTEL_B1 },
      "token-1",
      expect.any(AbortSignal),
    );
    expect(deps.selection.load(USER_ID)).toEqual({ orgId: ORG_B, hotelId: HOTEL_B1 });
  });

  it("restores the remembered hotel when the user still has it", async () => {
    const { deps } = fakeDeps();
    deps.selection.save(USER_ID, { orgId: ORG_A, hotelId: HOTEL_A2 });
    render(<HarnessApp deps={deps} />);
    const select = (await screen.findByRole("combobox", { name: "Hotel" })) as HTMLSelectElement;
    expect(select.value).toBe(HOTEL_A2);
  });

  it("ignores a remembered hotel the user no longer has", async () => {
    const { deps } = fakeDeps({ hotels: [ACCESS_A1] });
    deps.selection.save(USER_ID, { orgId: ORG_A, hotelId: HOTEL_A2 });
    render(<HarnessApp deps={deps} />);
    const select = (await screen.findByRole("combobox", { name: "Hotel" })) as HTMLSelectElement;
    expect(select.value).toBe(HOTEL_A1);
    await screen.findByText(/as operator/);
    expect(deps.api.getMe).not.toHaveBeenCalledWith(
      expect.objectContaining({ hotelId: HOTEL_A2 }),
      expect.anything(),
      expect.anything(),
    );
  });

  it("shows a late answer for the old hotel never over the new one", async () => {
    const { deps } = fakeDeps();
    const slow = deferred<MeResponse>();
    deps.api.getMe.mockImplementation(async (tenant) =>
      tenant.hotelId === HOTEL_A1 ? slow.promise : meFor(tenant, "admin"),
    );
    render(<HarnessApp deps={deps} />);
    const select = await screen.findByRole("combobox", { name: "Hotel" });
    await userEvent.setup().selectOptions(select, HOTEL_A2);
    await screen.findByText("You work in Acme City (Acme Hotels) as admin.");

    await act(async () => slow.resolve(meFor({ orgId: ORG_A, hotelId: HOTEL_A1 }, "owner")));
    expect(screen.queryByText(/as owner/)).toBeNull();
    expect(screen.getByText("You work in Acme City (Acme Hotels) as admin.")).toBeDefined();
  });

  it("tells a user with no memberships how to get access, and calls no API", async () => {
    const { deps } = fakeDeps({ hotels: [] });
    render(<HarnessApp deps={deps} />);
    expect(
      await screen.findByText(
        "Your account has no hotel access yet. Ask an admin of your organization to add you.",
      ),
    ).toBeDefined();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(deps.api.getMe).not.toHaveBeenCalled();
  });

  it("shows an error and loads the list again on retry", async () => {
    const { deps } = fakeDeps();
    deps.hotels.listMyHotels.mockRejectedValueOnce(new HotelDirectoryError());
    render(<HarnessApp deps={deps} />);

    expect((await screen.findByRole("alert")).textContent).toContain("Could not load your hotels.");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("combobox", { name: "Hotel" })).toBeDefined();
    expect(deps.hotels.listMyHotels).toHaveBeenCalledTimes(2);
  });
});

describe("API errors for the chosen hotel", () => {
  it("shows one plain message for a 403", async () => {
    const { deps } = fakeDeps();
    deps.api.getMe.mockRejectedValue(new HarnessApiError("forbidden", 403));
    render(<HarnessApp deps={deps} />);
    expect((await screen.findByRole("alert")).textContent).toBe(
      "You do not have access to this hotel. Ask an admin of your organization.",
    );
  });

  it.each([new HarnessApiError("unavailable", 503), new HarnessApiError("network")])(
    "shows that the API is not available (%s) and retries",
    async (error) => {
      const { deps } = fakeDeps();
      deps.api.getMe.mockRejectedValueOnce(error);
      render(<HarnessApp deps={deps} />);
      expect((await screen.findByRole("alert")).textContent).toContain(
        "The harness API is not available. Try again later.",
      );
      await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
      expect(await screen.findByText(/as operator/)).toBeDefined();
    },
  );

  it("signs out locally on a 401 and asks the user to sign in again", async () => {
    const { deps } = fakeDeps();
    deps.api.getMe.mockRejectedValue(new HarnessApiError("unauthenticated", 401));
    render(<HarnessApp deps={deps} />);

    expect(await screen.findByText("Your session ended. Sign in again.")).toBeDefined();
    expect(deps.auth.signOut).toHaveBeenCalledOnce();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeDefined();
  });

  it("treats a missing access token as an ended session without calling the API", async () => {
    const { deps, authControl } = fakeDeps();
    authControl.setToken(null);
    render(<HarnessApp deps={deps} />);
    expect(await screen.findByText("Your session ended. Sign in again.")).toBeDefined();
    expect(deps.api.getMe).not.toHaveBeenCalled();
  });
});

describe("startup", () => {
  it("shows a loading state until the session is known", async () => {
    const { deps } = fakeDeps();
    const pending = deferred<typeof USER | null>();
    deps.auth.currentUser.mockReturnValueOnce(pending.promise);
    render(<HarnessApp deps={deps} />);
    expect(screen.getByText("Loading…")).toBeDefined();
    await act(async () => pending.resolve(null));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeDefined();
  });

  it("keeps an auth event that arrives before the first session read", async () => {
    const { deps, authControl } = fakeDeps({ user: null });
    const pending = deferred<typeof USER | null>();
    deps.auth.currentUser.mockReturnValueOnce(pending.promise);
    render(<HarnessApp deps={deps} />);
    act(() => authControl.emit(USER));
    await act(async () => pending.resolve(null));
    expect(await screen.findByRole("combobox", { name: "Hotel" })).toBeDefined();
  });
});
