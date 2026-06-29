"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Topbar from "@/components/Topbar";
import Calendar from "@/components/Calendar";
import DayModal from "@/components/DayModal";
import { createClient } from "@/lib/supabase/client";
import { periodLabel } from "@/lib/month";
import { rollup } from "@/lib/engine";

export default function AdminClient({ profile, profiles, edits, timesheets, files, adminEdits }) {
  const supabase = createClient();
  const router = useRouter();
  const pmap = useMemo(() => Object.fromEntries(profiles.map((p) => [p.id, p])), [profiles]);
  const [tab, setTab] = useState("submissions");
  const [detail, setDetail] = useState(null);

  const totalHours = edits.reduce((a, e) => a + (e.fields?.totals?.total || 0), 0);
  const flagged = edits.filter((e) => (e.validation?.errors?.length || 0) > 0).length;

  return (
    <>
      <Topbar profile={profile} active="admin" />
      <div className="container" style={{ padding: "22px 24px 60px" }}>
        <h1 style={{ fontSize: 22, marginBottom: 4 }}>Admin console</h1>
        <p className="muted" style={{ marginBottom: 18 }}>
          Review employee submissions, audit edits, and make corrections.
        </p>

        <div className="tiles" style={{ marginBottom: 20 }}>
          <div className="tile"><div className="v">{profiles.length}</div><div className="l">Employees</div></div>
          <div className="tile"><div className="v">{edits.length}</div><div className="l">Submissions</div></div>
          <div className="tile tot"><div className="v">{Math.round(totalHours)}</div><div className="l">Total hours</div></div>
          <div className="tile"><div className="v" style={{ color: flagged ? "var(--red)" : "var(--green)" }}>{flagged}</div><div className="l">With errors</div></div>
        </div>

        <div className="tabs">
          {[["submissions", "Submissions"], ["employees", "Employees"], ["files", "Files"], ["revisions", "Admin revisions"]].map(([k, label]) => (
            <div key={k} className={"tab" + (tab === k ? " active" : "")} onClick={() => setTab(k)}>
              {label}
              {k === "revisions" && adminEdits.length > 0 && <span className="badge gray" style={{ marginLeft: 6 }}>{adminEdits.length}</span>}
            </div>
          ))}
        </div>

        {tab === "submissions" && (
          <Table headers={["Employee", "Client", "Period", "Regular", "OT", "Total", "Status", "Submitted", ""]}>
            {edits.length === 0 && <Empty cols={9} text="No submissions yet." />}
            {edits.map((e) => {
              const p = pmap[e.user_id] || {};
              const t = e.fields?.totals || {};
              const errs = e.validation?.errors?.length || 0;
              return (
                <tr key={e.id}>
                  <td><b>{p.full_name || e.fields?.employee_name || "—"}</b><br /><span className="muted" style={{ fontSize: 12 }}>{p.email}</span></td>
                  <td>{e.fields?.client || p.client || "—"}</td>
                  <td>{periodLabel(e.month, e.year)}</td>
                  <td>{t.regular ?? "—"}</td>
                  <td>{t.overtime ?? "—"}</td>
                  <td><b>{t.total ?? "—"}</b></td>
                  <td>{errs > 0 ? <span className="badge red">{errs} error{errs > 1 ? "s" : ""}</span> : <span className="badge green">clean</span>}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{fmt(e.created_at)}</td>
                  <td><button className="btn btn-ghost btn-sm" onClick={() => setDetail(e)}>Review</button></td>
                </tr>
              );
            })}
          </Table>
        )}

        {tab === "employees" && (
          <Table headers={["Name", "Email", "Role", "Employer", "Client", "Job title", "Manager"]}>
            {profiles.map((p) => (
              <tr key={p.id}>
                <td><b>{p.full_name || "—"}</b></td>
                <td>{p.email}</td>
                <td>{p.role === "admin" ? <span className="badge purple">admin</span> : <span className="badge gray">employee</span>}</td>
                <td>{p.employer || "—"}</td>
                <td>{p.client || "—"}</td>
                <td>{p.job_title || "—"}</td>
                <td>{p.manager_name || "—"}</td>
              </tr>
            ))}
          </Table>
        )}

        {tab === "files" && (
          <Table headers={["Employee", "File", "Period", "Type", "Size", "Uploaded", ""]}>
            {files.length === 0 && <Empty cols={7} text="No files uploaded yet." />}
            {files.map((f) => {
              const p = pmap[f.user_id] || {};
              return (
                <tr key={f.id}>
                  <td>{p.full_name || "—"}</td>
                  <td>{f.file_name}</td>
                  <td>{periodLabel(f.month, f.year)}</td>
                  <td className="muted">{f.mime_type || "—"}</td>
                  <td className="muted">{f.size_bytes ? Math.round(f.size_bytes / 1024) + " KB" : "—"}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{fmt(f.created_at)}</td>
                  <td><DownloadBtn supabase={supabase} path={f.storage_path} /></td>
                </tr>
              );
            })}
          </Table>
        )}

        {tab === "revisions" && (
          <Table headers={["Employee", "Period", "Edited by admin", "Note", "When"]}>
            {adminEdits.length === 0 && <Empty cols={5} text="No admin revisions yet." />}
            {adminEdits.map((a) => {
              const p = pmap[a.employee_user_id] || {};
              const ad = pmap[a.admin_user_id] || {};
              return (
                <tr key={a.id}>
                  <td><b>{p.full_name || "—"}</b></td>
                  <td>{periodLabel(a.month, a.year)}</td>
                  <td>{ad.full_name || "admin"}</td>
                  <td>{a.note || "—"}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{fmt(a.created_at)}</td>
                </tr>
              );
            })}
          </Table>
        )}
      </div>

      {detail && (
        <SubmissionDetail
          edit={detail} profile={pmap[detail.user_id] || {}} adminProfile={profile}
          sourceFile={files.find((f) => f.user_id === detail.user_id
            && f.month === detail.month && f.year === detail.year)}
          supabase={supabase} onClose={() => setDetail(null)}
          onSaved={() => { setDetail(null); router.refresh(); }}
        />
      )}
    </>
  );
}

function SubmissionDetail({ edit, profile, adminProfile, sourceFile, supabase, onClose, onSaved }) {
  const [days, setDays] = useState(edit.days || []);
  const [dayIdx, setDayIdx] = useState(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState(false);
  const r = rollup(days);
  const q = edit.questionnaire || {};

  async function saveAdminEdit() {
    setSaving(true);
    const { error } = await supabase.from("ts_admin_edits").insert({
      timesheet_id: edit.timesheet_id, employee_user_id: edit.user_id,
      admin_user_id: adminProfile.id, month: edit.month, year: edit.year,
      fields: { ...(edit.fields || {}), totals: r }, days,
      questionnaire: q, validation: edit.validation || {}, note: note || null,
    });
    setSaving(false);
    if (!error) { setSaved(true); setTimeout(() => (onSaved ? onSaved() : onClose()), 900); }
  }

  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h3 style={{ fontSize: 16 }}>{profile.full_name || edit.fields?.employee_name} · {periodLabel(edit.month, edit.year)}</h3>
            <div className="muted" style={{ fontSize: 12 }}>{profile.email} · {edit.fields?.client || profile.client || "—"}</div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            {sourceFile && (
              <button className="btn btn-ghost btn-sm" onClick={() => setPreview(true)} title="Verify against the original document">
                📄 Preview document
              </button>
            )}
            <button className="x" onClick={onClose}>×</button>
          </div>
        </div>
        <div className="modal-body">
          <div className="tiles" style={{ marginBottom: 16 }}>
            <div className="tile reg"><div className="v">{r.regular}</div><div className="l">Regular</div></div>
            <div className="tile ot"><div className="v">{r.overtime}</div><div className="l">Overtime</div></div>
            <div className="tile tot"><div className="v">{r.total}</div><div className="l">Total</div></div>
            <div className="tile"><div className="v">{r.daysWorked}</div><div className="l">Days worked</div></div>
          </div>

          {(edit.validation?.errors?.length > 0) && (
            <div className="alert error" style={{ marginBottom: 14 }}>
              Employee submitted with {edit.validation.errors.length} unresolved error(s).
            </div>
          )}

          <h3 className="card-title">Questionnaire answers</h3>
          <div className="grid-2" style={{ marginBottom: 16 }}>
            <KV k="Regular (stated)" v={q.regularHours} />
            <KV k="Overtime (stated)" v={q.overtimeHours} />
            <KV k="Worked weekends" v={q.workedWeekends} />
            <KV k="Holidays taken" v={q.holidaysTaken} />
            <KV k="Holidays paid" v={q.holidaysPaid} />
            <KV k="PTO days" v={q.ptoDays} />
          </div>
          {q.notes && <div className="alert info" style={{ marginBottom: 16 }}>“{q.notes}”</div>}

          <h3 className="card-title">Calendar — click a day to correct as admin</h3>
          <Calendar calendar={days} month={edit.month} year={edit.year} onDayClick={setDayIdx} />

          <div className="field" style={{ marginTop: 16 }}>
            <label>Admin note (why you changed it)</label>
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Corrected Apr 14 — client confirmed 8h" />
          </div>
          <div className="between">
            <span className="muted" style={{ fontSize: 12 }}>Saved as a separate admin revision; the employee’s submission is preserved.</span>
            <div className="row">
              <button className="btn btn-ghost" onClick={onClose}>Close</button>
              <button className="btn btn-primary" disabled={saving} onClick={saveAdminEdit}>
                {saved ? "Saved ✓" : saving ? "Saving…" : "Save admin revision"}
              </button>
            </div>
          </div>
        </div>
      </div>

      {dayIdx != null && (
        <DayModal day={days[dayIdx]} onClose={() => setDayIdx(null)}
          onSave={(upd) => { const n = days.slice(); n[dayIdx] = upd; setDays(n); setDayIdx(null); }} />
      )}

      {preview && sourceFile && (
        <DocPreview supabase={supabase} path={sourceFile.storage_path}
          fileName={sourceFile.file_name} onClose={() => setPreview(false)} />
      )}
    </div>
  );
}

// Big-screen source-document preview: renders the stored file to scrollable page
// images (via the admin-preview route -> engine) so admins can verify a
// submission against the original. Zoom + open-in-new-tab supported.
function DocPreview({ supabase, path, fileName, onClose }) {
  const [pages, setPages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [zoom, setZoom] = useState(1);
  const clamp = (z) => Math.min(4, Math.max(0.4, z));

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const res = await fetch("/api/admin-preview", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error || "preview failed");
        if (active) setPages(d.pages || []);
      } catch (e) {
        if (active) setErr(String(e.message || e));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [path]);

  async function openOriginal() {
    const { data } = await supabase.storage.from("ts-uploads").createSignedUrl(path, 120);
    if (data?.signedUrl) window.open(data.signedUrl, "_blank");
  }

  return (
    <div className="modal-bg" style={{ zIndex: 70 }} onClick={onClose}>
      <div className="docpreview" onClick={(e) => e.stopPropagation()}>
        <div className="pv-bar">
          <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            📄 {fileName || "Source document"}
          </span>
          <div className="row" style={{ gap: 4 }}>
            <button className="btn btn-ghost btn-sm" onClick={() => setZoom((z) => clamp(z * 0.8))} title="Zoom out">−</button>
            <span className="muted" style={{ fontSize: 11, minWidth: 38, textAlign: "center" }}>{Math.round(zoom * 100)}%</span>
            <button className="btn btn-ghost btn-sm" onClick={() => setZoom((z) => clamp(z * 1.25))} title="Zoom in">+</button>
            <button className="btn btn-ghost btn-sm" onClick={() => setZoom(1)} title="Fit">⤢</button>
            <button className="btn btn-ghost btn-sm" onClick={openOriginal} title="Open original in a new tab">open ↗</button>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>×</button>
          </div>
        </div>
        <div className="pv-body" style={{ "--z": zoom }}>
          {loading && (
            <div style={{ color: "#e2e8f0", textAlign: "center", padding: 40, fontSize: 13 }}>
              <span className="spinner" style={{ marginRight: 8 }} /> Rendering document…
            </div>
          )}
          {err && (
            <div style={{ color: "#fca5a5", textAlign: "center", padding: 40, fontSize: 13 }}>
              Couldn’t render preview: {err}<br />
              <a className="src-link" onClick={openOriginal} role="button" style={{ color: "#93c5fd" }}>Open the original ↗</a>
            </div>
          )}
          {!loading && !err && pages.map((src, i) => <img key={i} src={src} alt={`page ${i + 1}`} />)}
        </div>
      </div>
    </div>
  );
}

function DownloadBtn({ supabase, path }) {
  const [busy, setBusy] = useState(false);
  async function dl() {
    setBusy(true);
    const { data } = await supabase.storage.from("ts-uploads").createSignedUrl(path, 120);
    setBusy(false);
    if (data?.signedUrl) window.open(data.signedUrl, "_blank");
  }
  return <button className="btn btn-ghost btn-sm" disabled={busy} onClick={dl}>{busy ? "…" : "Download"}</button>;
}

function Table({ headers, children }) {
  return (
    <div className="card" style={{ overflow: "auto" }}>
      <table className="tbl">
        <thead><tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
function Empty({ cols, text }) {
  return <tr><td colSpan={cols} style={{ textAlign: "center", padding: 30, color: "var(--muted)" }}>{text}</td></tr>;
}
function KV({ k, v }) {
  return <div className="field" style={{ marginBottom: 6 }}><label>{k}</label><div>{v ?? "—"}</div></div>;
}
function fmt(ts) {
  if (!ts) return "—";
  try { return new Date(ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); }
  catch { return ts; }
}
