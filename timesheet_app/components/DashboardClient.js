"use client";
import { useMemo, useRef, useState } from "react";
import Topbar from "@/components/Topbar";
import Calendar from "@/components/Calendar";
import DayModal from "@/components/DayModal";
import Questionnaire from "@/components/Questionnaire";
import PreviewPane from "@/components/PreviewPane";
import { createClient } from "@/lib/supabase/client";
import { defaultPeriod, periodLabel, MONTHS } from "@/lib/month";
import { holidaysInMonth } from "@/lib/holidays";
import { buildCalendar, rollup } from "@/lib/engine";
import { validateTimesheet } from "@/lib/validate";

export default function DashboardClient({ profile }) {
  const supabase = createClient();
  const uid = profile.id;
  const fileInput = useRef(null);

  const [period, setPeriod] = useState(defaultPeriod());
  const { month, year } = period;
  const holidays = useMemo(() => holidaysInMonth(year, month), [year, month]);

  const [mode, setMode] = useState("upload"); // upload | review
  const [file, setFile] = useState(null);
  const [previewPages, setPreviewPages] = useState([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [showPreview, setShowPreview] = useState(false);
  const [drag, setDrag] = useState(false);

  const [processing, setProcessing] = useState(false);
  const [processError, setProcessError] = useState("");
  const [aiMeta, setAiMeta] = useState(null);

  const [fields, setFields] = useState({
    employee_name: profile.full_name || "", employee_id: profile.employee_code || "",
    client: profile.client || "", project: "", employer: profile.employer || "",
  });
  const [calendar, setCalendar] = useState([]);
  const [q, setQ] = useState({});
  const [holidayWork, setHolidayWork] = useState({});
  const [dayIdx, setDayIdx] = useState(null);

  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState("");
  const [timesheetId, setTimesheetId] = useState(null);

  // live validation + totals
  const validation = useMemo(
    () => validateTimesheet({ fields, calendar, questionnaire: q, holidayWork, holidays }),
    [fields, calendar, q, holidayWork, holidays]
  );
  const totals = {
    regular: validation.calReg, overtime: validation.calOt,
    total: validation.calTotal, weekendHrs: validation.weekendHrs,
  };
  const setField = (k) => (e) => setFields({ ...fields, [k]: e.target.value });

  // ---------- file selection + preview ----------
  async function onPickFile(f) {
    if (!f) return;
    setFile(f);
    setShowPreview(true);
    setPreviewLoading(true);
    setPreviewPages([]);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const res = await fetch("/api/preview", { method: "POST", body: fd });
      const data = await res.json();
      setPreviewPages(data.pages || []);
    } catch {
      setPreviewPages([]);
    } finally {
      setPreviewLoading(false);
    }
  }

  // ---------- AI processing ----------
  async function processAI() {
    if (!file) return;
    setProcessing(true);
    setProcessError("");
    let storagePath = null;
    try {
      // 1) keep the source in storage
      const ext = file.name.includes(".") ? file.name.split(".").pop() : "bin";
      storagePath = `${uid}/${year}-${String(month).padStart(2, "0")}/${Date.now()}.${ext}`;
      await supabase.storage.from("ts-uploads").upload(storagePath, file, {
        contentType: file.type || "application/octet-stream", upsert: true,
      });

      // 2) run the engine
      const fd = new FormData();
      fd.append("file", file);
      fd.append("month", String(month));
      fd.append("year", String(year));
      const res = await fetch("/api/process", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || "processing failed");

      const emp = data.employee;
      const cal = data.calendar;
      setCalendar(cal);
      if (emp) {
        setFields((prev) => ({
          ...prev,
          employee_name: emp.employee_name || prev.employee_name,
          employee_id: emp.employee_id || prev.employee_id,
          client: (emp.clients && emp.clients[0]) || prev.client,
          project: (emp.projects && emp.projects[0]) || prev.project,
        }));
      }
      const t = data.totals || rollup(cal);
      setQ((prev) => ({
        ...prev,
        regularHours: t.regular,
        overtimeHours: t.overtime,
        workedWeekends: cal.some((c) => c.isWeekend && c.total > 0) ? "yes" : "no",
      }));
      // default holiday-worked from the calendar
      const hw = {};
      for (const date of Object.keys(holidays)) {
        const c = cal.find((x) => x.date === date);
        hw[date] = !!(c && c.total > 0);
      }
      setHolidayWork(hw);
      setAiMeta({
        confidence: emp?.confidence, llm_used: data.llm_used,
        count: data.employee_count, fileName: data.file_name,
      });

      // 3) persist the AI baseline
      await saveBaseline({
        cal, emp, storagePath, file,
        aiStatus: emp ? "ok" : "failed", confidence: emp?.confidence ?? null,
      });
      setMode("review");
    } catch (e) {
      setProcessError(String(e.message || e));
    } finally {
      setProcessing(false);
    }
  }

  function startManual() {
    setCalendar(buildCalendar(null, month, year));
    setQ({ regularHours: "", overtimeHours: "", workedWeekends: "" });
    setHolidayWork({});
    setAiMeta(null);
    setMode("review");
  }

  // ---------- persistence ----------
  async function saveBaseline({ cal, emp, storagePath, file, aiStatus, confidence }) {
    let fileId = null;
    if (storagePath && file) {
      const { data: fr } = await supabase
        .from("ts_files")
        .insert({
          user_id: uid, month, year, file_name: file.name,
          storage_path: storagePath, mime_type: file.type || null,
          size_bytes: file.size || null, status: "processed",
        })
        .select("id").single();
      fileId = fr?.id || null;
    }
    const r = rollup(cal);
    const { data: tr } = await supabase
      .from("ts_timesheets")
      .upsert(
        {
          user_id: uid, file_id: fileId, month, year,
          employee_name: emp?.employee_name || fields.employee_name || null,
          employee_id: emp?.employee_id || fields.employee_id || null,
          client: (emp?.clients && emp.clients[0]) || fields.client || null,
          projects: emp?.projects || null,
          monthly_regular: r.regular, monthly_overtime: r.overtime,
          monthly_total: r.total, days_worked: r.daysWorked,
          days: cal, questionnaire: {}, validation: {},
          ai_confidence: confidence, ai_status: aiStatus,
        },
        { onConflict: "user_id,year,month" }
      )
      .select("id").single();
    if (tr?.id) setTimesheetId(tr.id);
    return tr?.id;
  }

  async function ensureTimesheet() {
    if (timesheetId) return timesheetId;
    return saveBaseline({
      cal: calendar, emp: null, storagePath: null, file: null,
      aiStatus: "failed", confidence: null,
    });
  }

  async function submit() {
    if (!validation.ok) return;
    setSaving(true);
    setSavedMsg("");
    try {
      const tid = await ensureTimesheet();
      const r = rollup(calendar);
      const { error } = await supabase.from("ts_employee_edits").insert({
        timesheet_id: tid, user_id: uid, month, year,
        fields: { ...fields, totals: r },
        days: calendar,
        questionnaire: { ...q, holidayWork },
        validation: { errors: validation.errors, warnings: validation.warnings },
        submitted: true,
      });
      if (error) throw error;
      // keep ts_timesheets totals in sync with the latest edit
      await supabase.from("ts_timesheets").update({
        monthly_regular: r.regular, monthly_overtime: r.overtime,
        monthly_total: r.total, days_worked: r.daysWorked,
        questionnaire: { ...q, holidayWork },
        validation: { errors: validation.errors, warnings: validation.warnings },
      }).eq("id", tid);
      setSavedMsg("Timesheet submitted ✓  Your manager can now review it.");
    } catch (e) {
      setProcessError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  function resetForNew() {
    setMode("upload"); setFile(null); setPreviewPages([]); setShowPreview(false);
    setCalendar([]); setQ({}); setHolidayWork({}); setAiMeta(null);
    setProcessError(""); setSavedMsg(""); setTimesheetId(null);
  }

  // ---------- render ----------
  return (
    <>
      <Topbar profile={profile} active="dashboard" />
      <div className="container" style={{ padding: "22px 24px 60px" }}>
        <div className="between" style={{ marginBottom: 18, flexWrap: "wrap", gap: 12 }}>
          <div>
            <h1 style={{ fontSize: 22 }}>My Timesheet</h1>
            <p className="muted" style={{ marginTop: 2 }}>
              Period auto-selected for you — <b>{periodLabel(month, year)}</b>.
              {new Date().getDate() <= 10 && " (Within the grace window, so last month is shown.)"}
            </p>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <select value={month} onChange={(e) => setPeriod({ ...period, month: +e.target.value })} disabled={mode === "review"}>
              {MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
            </select>
            <input type="number" value={year} style={{ width: 90 }}
              onChange={(e) => setPeriod({ ...period, year: +e.target.value })} disabled={mode === "review"} />
          </div>
        </div>

        {savedMsg && <div className="alert ok" style={{ marginBottom: 16 }}>{savedMsg}
          <a style={{ marginLeft: "auto" }} onClick={resetForNew} role="button">Start another</a></div>}

        {mode === "upload" && (
          <UploadStep
            file={file} drag={drag} setDrag={setDrag} fileInput={fileInput}
            onPickFile={onPickFile} processing={processing} processAI={processAI}
            startManual={startManual} processError={processError}
            previewPages={previewPages} previewLoading={previewLoading}
          />
        )}

        {mode === "review" && (
          <ReviewStep
            fields={fields} setField={setField} calendar={calendar} month={month} year={year}
            onDayClick={setDayIdx} validation={validation} totals={totals}
            q={q} setQ={setQ} holidays={holidays} holidayWork={holidayWork} setHolidayWork={setHolidayWork}
            aiMeta={aiMeta} saving={saving} submit={submit}
            showPreview={showPreview && previewPages.length > 0}
            previewPages={previewPages} previewLoading={previewLoading}
            fileName={file?.name} togglePreview={() => setShowPreview((s) => !s)}
            resetForNew={resetForNew}
          />
        )}
      </div>

      {dayIdx != null && (
        <DayModal
          day={calendar[dayIdx]}
          onClose={() => setDayIdx(null)}
          onSave={(upd) => {
            const next = calendar.slice();
            next[dayIdx] = upd;
            setCalendar(next);
            setDayIdx(null);
          }}
        />
      )}
    </>
  );
}

// ---------------- upload step ----------------
function UploadStep({ file, drag, setDrag, fileInput, onPickFile, processing, processAI, startManual, processError, previewPages, previewLoading }) {
  return (
    <div className="split">
      <div className="stack">
        <div className="card card-pad">
          <h3 className="card-title">1 · Upload your timesheet</h3>
          <div
            className={"dropzone" + (drag ? " drag" : "")}
            onClick={() => fileInput.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); onPickFile(e.dataTransfer.files?.[0]); }}
          >
            <input ref={fileInput} type="file" hidden
              accept=".pdf,.png,.jpg,.jpeg,.xlsx,.xls,.csv,.docx,.doc"
              onChange={(e) => onPickFile(e.target.files?.[0])} />
            <div style={{ fontSize: 30 }}>📄</div>
            <div style={{ fontWeight: 600, marginTop: 6 }}>
              {file ? file.name : "Drop a file or click to browse"}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              PDF, scanned PDF, Excel, CSV, Word, or an image
            </div>
          </div>

          {processError && <div className="alert error" style={{ marginTop: 14 }}>{processError}</div>}

          <div className="row" style={{ marginTop: 16, gap: 10 }}>
            <button className="btn btn-primary" disabled={!file || processing} onClick={processAI}>
              {processing ? <><span className="spinner" /> Processing with AI…</> : "✨ Process with AI"}
            </button>
            <button className="btn btn-ghost" onClick={startManual} disabled={processing}>
              Enter manually instead
            </button>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            The AI reads your document and fills the calendar + details. You can fix anything it misses on the next screen.
          </p>
        </div>
      </div>

      {file && (
        <PreviewPane pages={previewPages} loading={previewLoading} fileName={file.name} />
      )}
    </div>
  );
}

// ---------------- review step ----------------
function ReviewStep({
  fields, setField, calendar, month, year, onDayClick, validation, totals,
  q, setQ, holidays, holidayWork, setHolidayWork, aiMeta, saving, submit,
  showPreview, previewPages, previewLoading, fileName, togglePreview, resetForNew,
}) {
  const left = (
    <div className="stack">
      {aiMeta && (
        <div className="alert info">
          ✨ AI populated this from <b>{aiMeta.fileName}</b>
          {aiMeta.confidence != null && <> · confidence {Math.round(aiMeta.confidence * 100)}%</>}
          {aiMeta.llm_used ? " · LLM used" : ""}. Review and correct anything below.
        </div>
      )}

      {/* validation banner */}
      {validation.errors.length > 0 ? (
        <div className="alert error">
          <div>
            <b>Please fix {validation.errors.length} issue{validation.errors.length > 1 ? "s" : ""} before submitting:</b>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {validation.errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          </div>
        </div>
      ) : (
        <div className="alert ok">✓ Calendar and answers match. Ready to submit.</div>
      )}
      {validation.warnings.length > 0 && (
        <div className="alert warn">
          <div>
            <b>Heads up:</b>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {validation.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          </div>
        </div>
      )}

      {/* totals */}
      <div className="tiles">
        <div className="tile reg"><div className="v">{totals.regular}</div><div className="l">Regular hrs</div></div>
        <div className="tile ot"><div className="v">{totals.overtime}</div><div className="l">Overtime hrs</div></div>
        <div className="tile tot"><div className="v">{totals.total}</div><div className="l">Total hrs</div></div>
        <div className="tile"><div className="v">{calendar.filter((c) => c.total > 0).length}</div><div className="l">Days worked</div></div>
      </div>

      {/* identity fields */}
      <div className="card card-pad">
        <h3 className="card-title">Details {aiMeta ? "(AI-filled — edit if wrong)" : ""}</h3>
        <div className="grid-2">
          <Field label="Employee name"><input value={fields.employee_name} onChange={setField("employee_name")} /></Field>
          <Field label="Employee ID / code"><input value={fields.employee_id} onChange={setField("employee_id")} /></Field>
          <Field label="Client / placement"><input value={fields.client} onChange={setField("client")} /></Field>
          <Field label="Project"><input value={fields.project} onChange={setField("project")} /></Field>
        </div>
      </div>

      {/* calendar */}
      <div className="card card-pad">
        <div className="between" style={{ marginBottom: 10 }}>
          <h3 className="card-title" style={{ margin: 0 }}>Calendar — click any day to edit</h3>
          <div className="row" style={{ gap: 8 }}>
            {previewPages.length > 0 && (
              <button className="btn btn-ghost btn-sm" onClick={togglePreview}>
                {showPreview ? "Hide source" : "📄 Show source"}
              </button>
            )}
          </div>
        </div>
        <Legend />
        <Calendar calendar={calendar} month={month} year={year} onDayClick={onDayClick} />
      </div>

      {/* questionnaire */}
      <Questionnaire q={q} setQ={setQ} holidays={holidays} holidayWork={holidayWork}
        setHolidayWork={setHolidayWork} calendar={calendar} totals={totals} />

      {/* submit */}
      <div className="card card-pad between">
        <div className="muted" style={{ fontSize: 13 }}>
          {validation.ok ? "Everything checks out." : "Resolve the errors above to enable submit."}
        </div>
        <div className="row">
          <button className="btn btn-ghost" onClick={resetForNew}>Start over</button>
          <button className="btn btn-primary" disabled={!validation.ok || saving} onClick={submit}>
            {saving ? <><span className="spinner" /> Submitting…</> : "Submit timesheet"}
          </button>
        </div>
      </div>
    </div>
  );

  if (showPreview) {
    return (
      <div className="split">
        {left}
        <PreviewPane pages={previewPages} loading={previewLoading} fileName={fileName} onClose={togglePreview} />
      </div>
    );
  }
  return left;
}

function Field({ label, children }) {
  return <div className="field"><label>{label}</label>{children}</div>;
}
function Legend() {
  return (
    <div className="legend">
      <span><i className="swatch" style={{ background: "var(--surface)" }} /> Worked</span>
      <span><i className="swatch" style={{ background: "var(--surface-2)" }} /> Weekend</span>
      <span><i className="swatch" style={{ background: "var(--purple-soft)", borderColor: "#d8b4fe" }} /> Holiday</span>
      <span><i className="swatch" style={{ background: "#fffbeb", borderColor: "#fbbf24" }} /> Missing</span>
      <span><i className="swatch" style={{ background: "var(--surface)", borderColor: "var(--red)" }} /> Flagged</span>
    </div>
  );
}
