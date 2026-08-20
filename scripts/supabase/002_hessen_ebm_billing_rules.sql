-- Versionierte, kompilierte EBM-/Regionalregeln für hessen-ebm.
-- Diese Migration wird im Supabase SQL Editor oder per Supabase CLI ausgeführt.

create table if not exists hessen_ebm_rule_sets (
  rule_set_key text primary key,
  rule_set_id text not null,
  version text not null,
  schema_version integer not null,
  quarter text not null,
  region text not null default 'Hessen',
  status text not null default 'publishing'
    check (status in ('publishing', 'active', 'superseded', 'failed')),
  source_catalog_id text,
  source_data_stand text,
  source_hash text not null,
  compiled_at timestamptz not null default now(),
  activated_at timestamptz,
  core_payload jsonb not null,
  summary jsonb not null default '{}'::jsonb,
  unique (rule_set_id, version, quarter, region)
);

create table if not exists hessen_ebm_rule_definitions (
  definition_key text primary key,
  rule_set_key text not null references hessen_ebm_rule_sets(rule_set_key) on delete cascade,
  rule_id text not null,
  definition_type text not null default 'catalog_validation',
  source_type text not null,
  source_catalog_id text,
  quarter text not null,
  region text not null,
  catalog_key text not null,
  gop text,
  gop_base text,
  title text not null,
  valid_from text,
  valid_to text,
  coverage_status text not null
    check (coverage_status in ('structured', 'partial', 'text_only')),
  machine_clause_count integer not null default 0,
  review_clause_count integer not null default 0,
  source_text text not null,
  source_reference jsonb not null default '{}'::jsonb,
  scope jsonb not null default '{}'::jsonb,
  definition jsonb not null,
  created_at timestamptz not null default now(),
  unique (rule_set_key, rule_id)
);

-- Macht die Migration auch dann wiederholbar, wenn eine frühere Fassung der
-- Regeltabelle bereits ohne Kontextspalten angelegt wurde.
alter table hessen_ebm_rule_definitions
  add column if not exists catalog_key text;
alter table hessen_ebm_rule_definitions
  add column if not exists scope jsonb not null default '{}'::jsonb;
update hessen_ebm_rule_definitions
  set catalog_key = coalesce(gop, rule_id)
  where catalog_key is null;
alter table hessen_ebm_rule_definitions
  alter column catalog_key set not null;
alter table hessen_ebm_rule_definitions
  alter column gop drop not null;
alter table hessen_ebm_rule_definitions
  alter column gop_base drop not null;

create table if not exists hessen_ebm_rule_clauses (
  clause_key text primary key,
  definition_key text not null references hessen_ebm_rule_definitions(definition_key) on delete cascade,
  rule_set_key text not null references hessen_ebm_rule_sets(rule_set_key) on delete cascade,
  rule_id text not null,
  clause_index integer not null,
  clause_type text not null,
  scope text,
  parameters jsonb not null default '{}'::jsonb,
  source_text text not null,
  machine_executable boolean not null default false,
  review_required boolean not null default true,
  confidence numeric(4,3) not null default 0.500,
  created_at timestamptz not null default now(),
  unique (definition_key, clause_index)
);

create table if not exists hessen_ebm_rule_compile_runs (
  run_id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running'
    check (status in ('running', 'succeeded', 'failed')),
  quarter text not null,
  region text not null default 'Hessen',
  rule_set_key text references hessen_ebm_rule_sets(rule_set_key) on delete set null,
  source_catalog_path text,
  summary jsonb not null default '{}'::jsonb,
  error text
);

create index if not exists idx_hessen_ebm_rule_sets_active
  on hessen_ebm_rule_sets(quarter, region, status, compiled_at desc);

create index if not exists idx_hessen_ebm_rule_definitions_lookup
  on hessen_ebm_rule_definitions(quarter, region, gop_base);

create index if not exists idx_hessen_ebm_rule_definitions_context
  on hessen_ebm_rule_definitions(rule_set_key, definition_type);

create index if not exists idx_hessen_ebm_rule_definitions_set
  on hessen_ebm_rule_definitions(rule_set_key);

create index if not exists idx_hessen_ebm_rule_clauses_definition
  on hessen_ebm_rule_clauses(definition_key, clause_index);

create index if not exists idx_hessen_ebm_rule_clauses_type
  on hessen_ebm_rule_clauses(rule_set_key, clause_type);

create index if not exists idx_hessen_ebm_rule_compile_runs_created
  on hessen_ebm_rule_compile_runs(created_at desc);
