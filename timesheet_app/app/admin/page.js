import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import AdminClient from "@/components/AdminClient";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("ts_profiles").select("*").eq("id", user.id).single();
  if (!profile || profile.role !== "admin") {
    redirect("/dashboard");
  }

  const [{ data: profiles }, { data: edits }, { data: timesheets }, { data: files }, { data: adminEdits }] =
    await Promise.all([
      supabase.from("ts_profiles").select("*").order("full_name"),
      supabase.from("ts_employee_edits").select("*").order("created_at", { ascending: false }),
      supabase.from("ts_timesheets").select("*").order("created_at", { ascending: false }),
      supabase.from("ts_files").select("*").order("created_at", { ascending: false }),
      supabase.from("ts_admin_edits").select("*").order("created_at", { ascending: false }),
    ]);

  return (
    <AdminClient
      profile={profile}
      profiles={profiles || []}
      edits={edits || []}
      timesheets={timesheets || []}
      files={files || []}
      adminEdits={adminEdits || []}
    />
  );
}
