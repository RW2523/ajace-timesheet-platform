"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

export default function ResetPage() {
  const router = useRouter();
  const supabase = createClient();
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [valid, setValid] = useState(false);

  // The /auth/callback handler establishes a recovery session before sending us
  // here. Confirm a session exists so we know the link was valid.
  useEffect(() => {
    let active = true;
    supabase.auth.getUser().then(({ data }) => {
      if (!active) return;
      setValid(!!data.user);
      setReady(true);
    });
    return () => { active = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function onSubmit(e) {
    e.preventDefault();
    setErr("");
    if (pw.length < 6) return setErr("Password must be at least 6 characters.");
    if (pw !== pw2) return setErr("Passwords don’t match.");
    setBusy(true);
    const { error } = await supabase.auth.updateUser({ password: pw });
    setBusy(false);
    if (error) { setErr(error.message); return; }
    setOk(true);
    setTimeout(() => { router.replace("/dashboard"); router.refresh(); }, 1200);
  }

  return (
    <div className="center-wrap">
      <div className="card" style={{ width: 400 }}>
        <div className="card-pad">
          <div className="brand" style={{ marginBottom: 4 }}>
            <span className="logo">⏱</span><span>Ajace Timesheets</span>
          </div>
          <h2 style={{ fontSize: 22, marginTop: 14 }}>Set a new password</h2>

          {!ready && <p className="muted" style={{ marginTop: 10 }}><span className="spinner dark" /> Verifying link…</p>}

          {ready && !valid && (
            <div className="alert error" style={{ marginTop: 14 }}>
              This reset link is invalid or has expired. <a href="/forgot">Request a new one</a>.
            </div>
          )}

          {ready && valid && !ok && (
            <>
              <p className="muted" style={{ marginTop: 4, marginBottom: 18 }}>Choose a new password for your account.</p>
              {err && <div className="alert error" style={{ marginBottom: 14 }}>{err}</div>}
              <form onSubmit={onSubmit}>
                <div className="field">
                  <label>New password</label>
                  <input type="password" required value={pw} onChange={(e) => setPw(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
                </div>
                <div className="field">
                  <label>Confirm new password</label>
                  <input type="password" required value={pw2} onChange={(e) => setPw2(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
                </div>
                <button className="btn btn-primary btn-block" disabled={busy} style={{ marginTop: 6 }}>
                  {busy ? <span className="spinner" /> : "Update password"}
                </button>
              </form>
            </>
          )}

          {ok && <div className="alert ok" style={{ marginTop: 14 }}>Password updated ✓ Redirecting…</div>}
        </div>
      </div>
    </div>
  );
}
