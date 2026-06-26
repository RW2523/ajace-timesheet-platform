import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { processUpload, buildCalendar, rollup } from "@/lib/engine";

export const maxDuration = 300; // allow long LLM runs

export async function POST(request) {
  // must be signed in
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    return NextResponse.json({ error: "not authenticated" }, { status: 401 });
  }

  let form;
  try {
    form = await request.formData();
  } catch {
    return NextResponse.json({ error: "bad form data" }, { status: 400 });
  }
  const file = form.get("file");
  const month = parseInt(form.get("month"), 10);
  const year = parseInt(form.get("year"), 10);
  if (!file || !month || !year) {
    return NextResponse.json({ error: "file, month, year required" }, { status: 400 });
  }

  try {
    const result = await processUpload(file, file.name || "upload", month, year);
    const employee = result.employee;
    const calendar = buildCalendar(employee, month, year);
    return NextResponse.json({
      ok: true,
      employee,
      employee_count: result.employee_count,
      calendar,
      totals: rollup(calendar),
      llm_used: result.llm_used,
      file_name: result.file_name,
      raw_employees: result.employees,
    });
  } catch (e) {
    return NextResponse.json(
      { error: `processing failed: ${e.message || e}` },
      { status: 502 }
    );
  }
}
