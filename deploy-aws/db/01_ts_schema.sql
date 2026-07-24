-- =============================================================================
-- AJACE Timesheet (Direct++) — database schema
-- Faithful reproduction of the live `ts_*` schema (public), including RLS,
-- helper functions, triggers, the private storage bucket, and one security
-- hardening the live copy was missing (role-change guard — see bottom).
--
-- Applied post-boot by scripts/migrate.sh against the running Supabase stack.
-- Requires the Supabase base (auth.*, storage.*, auth.uid()) — always present
-- in the supabase/postgres image. Safe to re-run (idempotent).
-- =============================================================================

-- ---------- helper: is the caller a timesheet admin? -------------------------
create or replace function public.ts_is_admin()
returns boolean language sql stable security definer set search_path=public as $$
  select exists(select 1 from public.ts_profiles where id = auth.uid() and role = 'admin');
$$;

-- ---------- ts_profiles ------------------------------------------------------
create table if not exists public.ts_profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  email         text,
  full_name     text,
  phone         text,
  role          text not null default 'employee' check (role in ('employee','admin')),
  employer      text,
  client        text,
  job_title     text,
  employee_code text,
  country       text default 'US',
  manager_name  text,
  manager_email text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ---------- ts_files ---------------------------------------------------------
create table if not exists public.ts_files (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  month        int  not null check (month between 1 and 12),
  year         int  not null,
  file_name    text not null,
  storage_path text not null,
  mime_type    text,
  size_bytes   bigint,
  status       text not null default 'uploaded'
               check (status in ('uploaded','processing','processed','failed')),
  created_at   timestamptz not null default now()
);
create index if not exists ts_files_user_idx on public.ts_files(user_id, year, month);

-- ---------- ts_timesheets ----------------------------------------------------
create table if not exists public.ts_timesheets (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references auth.users(id) on delete cascade,
  file_id          uuid references public.ts_files(id) on delete set null,
  month            int  not null check (month between 1 and 12),
  year             int  not null,
  employee_name    text,
  employee_id      text,
  client           text,
  projects         text[],
  monthly_regular  numeric default 0,
  monthly_overtime numeric default 0,
  monthly_total    numeric default 0,
  days_worked      int default 0,
  days             jsonb default '[]'::jsonb,
  questionnaire    jsonb default '{}'::jsonb,
  validation       jsonb default '{}'::jsonb,
  ai_confidence    numeric,
  ai_status        text default 'ok' check (ai_status in ('ok','partial','failed')),
  created_at       timestamptz not null default now(),
  unique (user_id, year, month)
);
create index if not exists ts_timesheets_user_idx on public.ts_timesheets(user_id, year, month);

-- ---------- ts_employee_edits ------------------------------------------------
create table if not exists public.ts_employee_edits (
  id            uuid primary key default gen_random_uuid(),
  timesheet_id  uuid references public.ts_timesheets(id) on delete cascade,
  user_id       uuid not null references auth.users(id) on delete cascade,
  month         int not null,
  year          int not null,
  fields        jsonb default '{}'::jsonb,
  days          jsonb default '[]'::jsonb,
  questionnaire jsonb default '{}'::jsonb,
  validation    jsonb default '{}'::jsonb,
  submitted     boolean not null default false,
  created_at    timestamptz not null default now()
);
create index if not exists ts_employee_edits_idx on public.ts_employee_edits(user_id, year, month);

-- ---------- ts_admin_edits ---------------------------------------------------
create table if not exists public.ts_admin_edits (
  id               uuid primary key default gen_random_uuid(),
  timesheet_id     uuid references public.ts_timesheets(id) on delete cascade,
  employee_user_id uuid not null references auth.users(id) on delete cascade,
  admin_user_id    uuid not null references auth.users(id) on delete cascade,
  month            int not null,
  year             int not null,
  fields           jsonb default '{}'::jsonb,
  days             jsonb default '[]'::jsonb,
  questionnaire    jsonb default '{}'::jsonb,
  validation       jsonb default '{}'::jsonb,
  note             text,
  created_at       timestamptz not null default now()
);
create index if not exists ts_admin_edits_idx on public.ts_admin_edits(employee_user_id, year, month);

-- ---------- ts_app_settings (admin-tunable flags) ----------------------------
-- Seeded to 'direct_serverless' so the AI flow is Direct++ (no Python engine).
-- The Next app also forces this via DIRECT_SERVERLESS=true (belt + braces).
create table if not exists public.ts_app_settings (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);
insert into public.ts_app_settings(key, value) values ('ai_flow','direct_serverless')
  on conflict (key) do update set value = excluded.value, updated_at = now();

-- ---------- updated_at touch --------------------------------------------------
create or replace function public.ts_touch_updated_at()
returns trigger language plpgsql set search_path=public as $$
begin new.updated_at = now(); return new; end; $$;

drop trigger if exists ts_profiles_touch on public.ts_profiles;
create trigger ts_profiles_touch before update on public.ts_profiles
  for each row execute function public.ts_touch_updated_at();

-- ---------- auth.users hooks: provision profile + auto-confirm ----------------
-- Only fire for users created by THIS app (raw_user_meta_data.app='ajace_timesheets').
create or replace function public.ts_handle_new_user()
returns trigger language plpgsql security definer set search_path=public as $$
begin
  if coalesce(new.raw_user_meta_data->>'app','') = 'ajace_timesheets' then
    insert into public.ts_profiles
      (id, email, full_name, phone, role, employer, client, job_title,
       employee_code, country, manager_name, manager_email)
    values (
      new.id, new.email,
      coalesce(new.raw_user_meta_data->>'full_name',''),
      new.raw_user_meta_data->>'phone', 'employee',
      new.raw_user_meta_data->>'employer', new.raw_user_meta_data->>'client',
      new.raw_user_meta_data->>'job_title', new.raw_user_meta_data->>'employee_code',
      coalesce(new.raw_user_meta_data->>'country','US'),
      new.raw_user_meta_data->>'manager_name', new.raw_user_meta_data->>'manager_email')
    on conflict (id) do nothing;
  end if;
  return new;
end; $$;

create or replace function public.ts_autoconfirm_timesheet()
returns trigger language plpgsql security definer set search_path=public as $$
begin
  if coalesce(new.raw_user_meta_data->>'app','') = 'ajace_timesheets'
     and new.email_confirmed_at is null then
    new.email_confirmed_at = now();
  end if;
  return new;
end; $$;

drop trigger if exists ts_autoconfirm on auth.users;
create trigger ts_autoconfirm before insert on auth.users
  for each row execute function public.ts_autoconfirm_timesheet();

drop trigger if exists ts_on_auth_user_created on auth.users;
create trigger ts_on_auth_user_created after insert on auth.users
  for each row execute function public.ts_handle_new_user();

-- =============================================================================
-- Row Level Security
-- =============================================================================
alter table public.ts_profiles       enable row level security;
alter table public.ts_files          enable row level security;
alter table public.ts_timesheets     enable row level security;
alter table public.ts_employee_edits enable row level security;
alter table public.ts_admin_edits    enable row level security;
alter table public.ts_app_settings   enable row level security;

-- ts_profiles: users see/edit their own row; admins can read all
drop policy if exists ts_profiles_self_sel on public.ts_profiles;
create policy ts_profiles_self_sel on public.ts_profiles for select
  using (id = auth.uid() or public.ts_is_admin());
drop policy if exists ts_profiles_self_ins on public.ts_profiles;
create policy ts_profiles_self_ins on public.ts_profiles for insert
  with check (id = auth.uid());
drop policy if exists ts_profiles_self_upd on public.ts_profiles;
create policy ts_profiles_self_upd on public.ts_profiles for update
  using (id = auth.uid()) with check (id = auth.uid());

-- ts_files: owner full access; admins read
drop policy if exists ts_files_owner on public.ts_files;
create policy ts_files_owner on public.ts_files for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists ts_files_admin_sel on public.ts_files;
create policy ts_files_admin_sel on public.ts_files for select using (public.ts_is_admin());

-- ts_timesheets: owner full access; admins read
drop policy if exists ts_ts_owner on public.ts_timesheets;
create policy ts_ts_owner on public.ts_timesheets for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists ts_ts_admin_sel on public.ts_timesheets;
create policy ts_ts_admin_sel on public.ts_timesheets for select using (public.ts_is_admin());

-- ts_employee_edits: owner full access; admins read
drop policy if exists ts_ee_owner on public.ts_employee_edits;
create policy ts_ee_owner on public.ts_employee_edits for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists ts_ee_admin_sel on public.ts_employee_edits;
create policy ts_ee_admin_sel on public.ts_employee_edits for select using (public.ts_is_admin());

-- ts_admin_edits: admins only
drop policy if exists ts_ae_admin_all on public.ts_admin_edits;
create policy ts_ae_admin_all on public.ts_admin_edits for all
  using (public.ts_is_admin()) with check (public.ts_is_admin());

-- ts_app_settings: any authenticated user may read; only admins may write
drop policy if exists ts_settings_read on public.ts_app_settings;
create policy ts_settings_read on public.ts_app_settings for select
  using (auth.uid() is not null);
drop policy if exists ts_settings_admin_write on public.ts_app_settings;
create policy ts_settings_admin_write on public.ts_app_settings for all
  using (public.ts_is_admin()) with check (public.ts_is_admin());

-- =============================================================================
-- Storage: private 'ts-uploads' bucket + owner/admin policies
-- Files are keyed {userId}/{YYYY-MM}/{timestamp}.{ext} — folder[1] = the owner.
-- =============================================================================
insert into storage.buckets (id, name, public) values ('ts-uploads','ts-uploads', false)
  on conflict (id) do nothing;

drop policy if exists ts_uploads_owner on storage.objects;
create policy ts_uploads_owner on storage.objects for all
  using  (bucket_id = 'ts-uploads' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'ts-uploads' and (storage.foldername(name))[1] = auth.uid()::text);

drop policy if exists ts_uploads_admin_sel on storage.objects;
create policy ts_uploads_admin_sel on storage.objects for select
  using (bucket_id = 'ts-uploads' and public.ts_is_admin());

-- =============================================================================
-- SECURITY HARDENING (not in the original live copy)
-- The ts_profiles self-update policy lets a user edit their own row — including
-- the `role` column — which would allow self-escalation to admin. This
-- definer-proof BEFORE-UPDATE trigger blocks any role change unless the caller
-- is already an admin. auth.uid() is null for the service_role/postgres paths
-- used by migrations, so those are unaffected.
-- =============================================================================
create or replace function public.ts_guard_role_change()
returns trigger language plpgsql security definer set search_path=public as $$
begin
  if new.role is distinct from old.role
     and auth.uid() is not null
     and not public.ts_is_admin() then
    raise exception 'not allowed to change role';
  end if;
  return new;
end; $$;

drop trigger if exists ts_profiles_guard_role on public.ts_profiles;
create trigger ts_profiles_guard_role before update on public.ts_profiles
  for each row execute function public.ts_guard_role_change();
