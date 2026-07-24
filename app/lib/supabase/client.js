"use client";
// Drop-in replacement for the Supabase browser client — backed by our own
// AWS-native API routes. Same shape (`.auth`, `.from`, `.storage`) so existing
// client components keep working without edits.
import { makeBuilder } from "@/lib/aws/builder";

const JSON_HEADERS = { "Content-Type": "application/json" };
const wrapErr = (msg) => ({ message: msg || "request failed" });

async function dataRunner(st) {
  const res = await fetch("/api/data", {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(st),
  });
  return res.json().catch(() => ({ data: null, error: "bad response" }));
}

export function createClient() {
  return {
    auth: {
      async signInWithPassword({ email, password }) {
        const r = await fetch("/api/auth/login", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ email, password }) });
        const j = await r.json().catch(() => ({}));
        return r.ok ? { data: { user: j.user, session: {} }, error: null } : { data: null, error: wrapErr(j.error) };
      },
      async signUp({ email, password, options }) {
        const r = await fetch("/api/auth/signup", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ email, password, meta: options?.data || {} }) });
        const j = await r.json().catch(() => ({}));
        // signup sets the session cookie, so return a truthy session (already logged in)
        return r.ok ? { data: { user: j.user, session: {} }, error: null } : { data: null, error: wrapErr(j.error) };
      },
      async signOut() {
        await fetch("/api/auth/logout", { method: "POST" });
        return { error: null };
      },
      async getUser() {
        const r = await fetch("/api/auth/me");
        const j = await r.json().catch(() => ({ user: null }));
        return { data: { user: j.user || null } };
      },
      async resetPasswordForEmail(email) {
        await fetch("/api/auth/forgot", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ email }) });
        return { error: null };
      },
      // reset now happens via a token on the /reset page (see /api/auth/reset)
      async updateUser() { return { data: null, error: wrapErr("use the reset link") }; },
    },

    from: (table) => makeBuilder(table, dataRunner),

    storage: {
      from() {
        return {
          async upload(path, file) {
            const contentType = file?.type || "application/octet-stream";
            const s = await fetch("/api/storage/sign", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path, method: "put", contentType }) });
            const { url, error } = await s.json().catch(() => ({}));
            if (!url) return { data: null, error: wrapErr(error) };
            const put = await fetch(url, { method: "PUT", body: file, headers: { "Content-Type": contentType } });
            return put.ok ? { data: { path }, error: null } : { data: null, error: wrapErr("upload failed") };
          },
          async createSignedUrl(path) {
            const s = await fetch("/api/storage/sign", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path, method: "get" }) });
            const j = await s.json().catch(() => ({}));
            return j.url ? { data: { signedUrl: j.url }, error: null } : { data: null, error: wrapErr(j.error) };
          },
          async download(path) {
            const s = await fetch("/api/storage/sign", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ path, method: "get" }) });
            const j = await s.json().catch(() => ({}));
            if (!j.url) return { data: null, error: wrapErr(j.error) };
            const f = await fetch(j.url);
            return f.ok ? { data: await f.blob(), error: null } : { data: null, error: wrapErr("download failed") };
          },
          async remove(paths) {
            await fetch("/api/storage/remove", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ paths }) });
            return { data: null, error: null };
          },
        };
      },
    },
  };
}
