// Central Supabase config. Env vars win; the fallbacks are the *publishable* (anon)
// credentials of the shared AJACE project — safe to ship to the browser (data is
// protected by RLS) and kept as a boot fallback so a deploy without dashboard env
// still runs. No secret (service_role, DB password) is ever baked into source.
export const SUPABASE_URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL || "https://coaszrosqlhifcwxurwu.supabase.co";

export const SUPABASE_ANON_KEY =
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ||
  "sb_publishable_nO8n5IxHIdrZSYf6WN5Ixw_vlRcUOOl";
