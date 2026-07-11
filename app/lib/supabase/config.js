// Central Supabase config. Env vars win; the fallbacks let the app build & run
// on Vercel without dashboard env configuration. These are the *publishable*
// (anon) credentials — safe to ship to the browser; data is protected by RLS.
export const SUPABASE_URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL || "https://coaszrosqlhifcwxurwu.supabase.co";

export const SUPABASE_ANON_KEY =
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ||
  "sb_publishable_nO8n5IxHIdrZSYf6WN5Ixw_vlRcUOOl";
