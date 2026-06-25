#!/usr/bin/env bash
# ============================================================================
# ADR-053 — durable append-only audit_logs: DB roles bootstrap (devops, NOT a migration).
#
# Creates the two least-privilege login roles the app uses (separate runtime vs migration
# privileges so audit_logs can be made tamper-resistant AT THE DB LAYER, not only in app code):
#   * app_rw      — runtime role of the `api` container (DATABASE_URL). Least-privilege.
#   * app_migrate — migration role (DATABASE_URL_MIGRATE). Full DDL/DML; used ONLY by the
#                   one-shot `migrate` job (alembic upgrade head), never by the runtime api.
#
# WHY HERE (and not in a migration): CREATE ROLE / cluster-level GRANTs require privileges the
# migration role itself must not need (ADR-053 §Роли БД). The roles MUST exist BEFORE migration
# 0016 runs (0016 does `REVOKE ... FROM app_rw` + `GRANT INSERT,SELECT TO app_rw` + the
# audit_logs_no_mutate trigger). 0016 is authored by backend; this file is devops-owned.
#
# WHY A .sh (not .sql): the role passwords come from ENV (APP_RW_PASSWORD / APP_MIGRATE_PASSWORD)
# and must NOT be hardcoded. psql's `:'var'` interpolation does NOT work inside a `DO $$ ... $$`
# block, so we build the password literals here via psql's quote_literal-safe parameter passing
# (-v + an outer non-DO statement) and run the idempotent role create with format()/EXECUTE.
#
# ⚠️ EXECUTION MODEL — Postgres only runs /docker-entrypoint-initdb.d/* on the FIRST
#    initialization of an EMPTY data volume. On an EXISTING volume (data already present, e.g. a
#    live prod DB) this file is NEVER re-run. For such DBs apply the roles MANUALLY — see the
#    "CREATE ROLE — prod (existing DB)" runbook in docs/07-deployment.md §Роли БД.
#    The script is IDEMPOTENT (guarded CREATE ROLE) so re-running it by hand is safe.
#
# PASSWORDS — local-dev defaults below are PLACEHOLDERS only; prod sets real secrets via the
#    secret manager (APP_RW_PASSWORD / APP_MIGRATE_PASSWORD in .env). NEVER commit real secrets.
#    DATABASE_URL / DATABASE_URL_MIGRATE must embed the SAME passwords for the matching role.
# ============================================================================
set -euo pipefail

APP_RW_PASSWORD="${APP_RW_PASSWORD:-app_rw}"
APP_MIGRATE_PASSWORD="${APP_MIGRATE_PASSWORD:-app_migrate}"

# Connect as the superuser to the bootstrap DB (both provided by the postgres entrypoint env).
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v app_rw_password="$APP_RW_PASSWORD" \
     -v app_migrate_password="$APP_MIGRATE_PASSWORD" \
     -v dbname="$POSTGRES_DB" <<-'EOSQL'
	-- Idempotent role create. quote_literal() safely embeds the :'var' password (psql substitutes
	-- :'app_rw_password' OUTSIDE the dollar-quoted body, so it works here unlike inside a DO block).
	SELECT format('CREATE ROLE app_rw LOGIN PASSWORD %L', :'app_rw_password')
	WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_rw')
	\gexec
	ALTER ROLE app_rw WITH LOGIN PASSWORD :'app_rw_password';

	SELECT format('CREATE ROLE app_migrate LOGIN PASSWORD %L', :'app_migrate_password')
	WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_migrate')
	\gexec
	ALTER ROLE app_migrate WITH LOGIN PASSWORD :'app_migrate_password';

	-- --- Grants ---------------------------------------------------------------
	-- Connect to THIS bootstrap database (init runs connected to it).
	GRANT CONNECT ON DATABASE :"dbname" TO app_rw, app_migrate;
	-- app_migrate needs CREATE on the DATABASE so migration 0001 can run
	-- `CREATE EXTENSION IF NOT EXISTS pgcrypto` (extension creation requires DB-level CREATE,
	-- not just schema CREATE). app_rw is intentionally NOT granted this.
	GRANT CREATE ON DATABASE :"dbname" TO app_migrate;

	-- Schema usage. app_rw operates inside schema public; app_migrate must also CREATE objects
	-- there (DDL). Per-table privileges for app_rw (incl. the audit_logs REVOKE) are applied
	-- LATER by migration 0016 — this file only grants schema-level access so both roles connect
	-- and the migrate job (app_migrate) can run the full alembic chain.
	GRANT USAGE ON SCHEMA public TO app_rw;
	GRANT USAGE, CREATE ON SCHEMA public TO app_migrate;

	-- app_migrate: broad DML/DDL on existing + future objects.
	GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO app_migrate;
	GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO app_migrate;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_migrate;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app_migrate;

	-- app_rw: baseline DML on tables migrations create. Default privileges keyed to app_migrate
	-- (the creator) so every table/sequence it creates is automatically usable by app_rw. The
	-- audit_logs hardening (REVOKE UPDATE,DELETE,TRUNCATE) is applied afterwards by migration 0016,
	-- which narrows audit_logs back to INSERT,SELECT for app_rw.
	ALTER DEFAULT PRIVILEGES FOR ROLE app_migrate IN SCHEMA public
	    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
	ALTER DEFAULT PRIVILEGES FOR ROLE app_migrate IN SCHEMA public
	    GRANT USAGE, SELECT ON SEQUENCES TO app_rw;
EOSQL

echo "ADR-053: roles app_rw / app_migrate ensured."
