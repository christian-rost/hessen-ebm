-- Persistierte Rechnungsentwürfe für hessen-ebm.
-- Diese Migration wird im Supabase SQL Editor oder per Supabase CLI ausgeführt.

create table if not exists hessen_ebm_invoices (
  analysis_id text primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  source_filename text not null,
  source_sha256 text not null,
  status text not null,
  quarter text,
  treatment_start text,
  treatment_end text,
  region text,
  diagnosis text,
  line_count integer not null default 0,
  points_total integer not null default 0,
  amount_total_eur numeric(12,2) not null default 0,
  human_review_required boolean not null default true,
  storage_backend text not null default 'supabase',
  payload jsonb not null
);

create table if not exists hessen_ebm_invoice_items (
  id bigserial primary key,
  analysis_id text not null references hessen_ebm_invoices(analysis_id) on delete cascade,
  line integer not null,
  gop_original text not null,
  gop_base text not null,
  gop_suffix text,
  title text not null,
  catalog_source text not null,
  catalog_source_label text,
  catalog_id text,
  catalog_data_stand text,
  quarter text not null,
  service_date text,
  service_time text,
  quantity integer not null default 1,
  points integer,
  amount_eur numeric(12,2),
  rule_id text not null,
  confidence text not null,
  evidence_ids text[] not null default array[]::text[],
  evidence_pages integer[] not null default array[]::integer[],
  validation_status text not null,
  validation_notes text[] not null default array[]::text[],
  derivation_source text not null,
  semantic_reason text,
  semantic_catalog_candidates text[] not null default array[]::text[],
  payload jsonb not null,
  unique (analysis_id, line)
);

create index if not exists idx_hessen_ebm_invoices_created_at
  on hessen_ebm_invoices(created_at desc);

create index if not exists idx_hessen_ebm_invoices_source_sha256
  on hessen_ebm_invoices(source_sha256);

create index if not exists idx_hessen_ebm_invoices_quarter
  on hessen_ebm_invoices(quarter);

create index if not exists idx_hessen_ebm_invoice_items_analysis_id
  on hessen_ebm_invoice_items(analysis_id);

create index if not exists idx_hessen_ebm_invoice_items_gop_base
  on hessen_ebm_invoice_items(gop_base);
