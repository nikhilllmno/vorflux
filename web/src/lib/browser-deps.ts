// Builds the real dependencies for the browser. Tests pass fakes to <HarnessApp> instead.
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import { createSupabaseAuthGateway, type AuthGateway } from "@/lib/auth";
import type { PublicConfig } from "@/lib/config";
import { createHarnessApi, type HarnessApi } from "@/lib/harness-api";
import {
  createSupabaseHotelDirectory,
  type FilterBuilder,
  type HotelDirectory,
  type QueryResult,
  type TableReader,
} from "@/lib/hotel-directory";
import { createSelectionStore, type SelectionStore } from "@/lib/selection-store";

export interface HarnessDeps {
  auth: AuthGateway;
  hotels: HotelDirectory;
  api: HarnessApi;
  selection: SelectionStore;
}

function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

type PostgrestQuery = ReturnType<ReturnType<SupabaseClient["from"]>["select"]>;

/** Narrows the Supabase query builder to the calls the hotel directory makes. */
function filterBuilder(query: PostgrestQuery): FilterBuilder {
  return {
    eq: (column, value) => filterBuilder(query.eq(column, value)),
    in: (column, values) => filterBuilder(query.in(column, values)),
    then: (onFulfilled, onRejected) =>
      query
        .then((result: QueryResult): QueryResult => ({ data: result.data, error: result.error }))
        .then(onFulfilled, onRejected),
  };
}

function tableReader(supabase: SupabaseClient): TableReader {
  return {
    from: (table) => ({ select: (columns) => filterBuilder(supabase.from(table).select(columns)) }),
  };
}

/** Call in the browser only: supabase-js keeps the session in browser storage. */
export function createBrowserDeps(config: PublicConfig): HarnessDeps {
  const supabase = createClient(config.supabaseUrl, config.supabaseAnonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      // Email and password only: no redirect callbacks to parse (decision W2).
      detectSessionInUrl: false,
    },
  });
  return {
    auth: createSupabaseAuthGateway(supabase.auth),
    hotels: createSupabaseHotelDirectory(tableReader(supabase)),
    api: createHarnessApi({ baseUrl: config.harnessApiUrl }),
    selection: createSelectionStore(browserStorage()),
  };
}
