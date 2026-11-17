CREATE SCHEMA IF NOT EXISTS mt_admin;

CREATE TABLE IF NOT EXISTS mt_admin.tenant (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    collection  text NOT NULL,
    status      text NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mt_admin.api_key (
    id                  text PRIMARY KEY,
    agent               text NOT NULL,
    secret_hash         text NOT NULL,
    tenants             text[] NOT NULL DEFAULT '{}',
    access              text NOT NULL DEFAULT 'write',
    denied_tools        text[] NOT NULL DEFAULT '{}',
    source_repo_patterns text[] NOT NULL DEFAULT '{}',
    expires_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL DEFAULT '',
    revoked_at          timestamptz,
    last_used_at        timestamptz,
    rotated_from        text
);

CREATE TABLE IF NOT EXISTS mt_admin.role_mapping (
    group_id   text NOT NULL,
    tenant     text NOT NULL,
    role       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT '',
    UNIQUE (group_id, tenant, role)
);

CREATE TABLE IF NOT EXISTS mt_admin.setting (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mt_admin.audit_log (
    id             bigserial PRIMARY KEY,
    ts             timestamptz NOT NULL DEFAULT now(),
    principal_type text,
    principal_id   text,
    agent          text,
    acting_user    text,
    tenant         text,
    action         text NOT NULL,
    target         text,
    outcome        text,
    detail         jsonb
);

CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON mt_admin.audit_log (ts);
CREATE INDEX IF NOT EXISTS audit_log_tenant_ts_idx ON mt_admin.audit_log (tenant, ts);
