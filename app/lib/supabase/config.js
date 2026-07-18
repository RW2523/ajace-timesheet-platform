// Central Supabase config. The publishable (anon) credentials are safe to ship to the
// browser (data is protected by RLS) but MUST come from env — no Supabase project is
// baked into source, so this repo is not hard-wired to one project and no key lives in
// git. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY in Vercel
// (Settings → Environment Variables) or .env.local.
function required(name, value) {
  if (!value) {
    throw new Error(
      `Missing ${name}. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY ` +
        `in the environment (Vercel → Settings → Environment Variables, or .env.local).`,
    );
  }
  return value;
}

export const SUPABASE_URL = required(
  "NEXT_PUBLIC_SUPABASE_URL",
  process.env.NEXT_PUBLIC_SUPABASE_URL,
);

export const SUPABASE_ANON_KEY = required(
  "NEXT_PUBLIC_SUPABASE_ANON_KEY",
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
);
