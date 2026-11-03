-- ========================================
-- RDS Database Setup for Valkey Benchmark
-- ========================================
-- Creates two databases:
-- 1. grafana - for Grafana metrics and settings
-- 2. postgres - for valkey_benchmark_metrics
--
-- Creates two users:
-- 1. Admin (postgres) - full access to all databases
-- 2. github_actions - IAM-enabled user for GitHub Actions
-- ========================================

-- Create databases
SELECT 'CREATE DATABASE grafana' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'grafana')\gexec
SELECT 'CREATE DATABASE postgres' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'postgres')\gexec

-- Create IAM-enabled user for GitHub Actions
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_user WHERE usename = 'github_actions') THEN
    CREATE USER github_actions WITH LOGIN;
    RAISE NOTICE 'Created user: github_actions';
  ELSE
    RAISE NOTICE 'User github_actions already exists';
  END IF;
END
$$;

-- Grant rds_iam role for IAM authentication to github_actions
GRANT rds_iam TO github_actions;

-- Grant CREATE permission on public schema to github_actions
GRANT CREATE ON SCHEMA public TO github_actions;

-- Set github_actions as the owner for new objects in public schema
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO github_actions;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO github_actions;

-- Create benchmark_metrics table in postgres database (owned by github_actions)
CREATE TABLE IF NOT EXISTS benchmark_metrics (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    commit VARCHAR(40) NOT NULL,
    command VARCHAR(50) NOT NULL,
    data_size INTEGER,
    pipeline INTEGER,
    clients INTEGER,
    requests INTEGER,
    rps DECIMAL(12,2),
    avg_latency_ms DECIMAL(10,3),
    min_latency_ms DECIMAL(10,3),
    p50_latency_ms DECIMAL(10,3),
    p95_latency_ms DECIMAL(10,3),
    p99_latency_ms DECIMAL(10,3),
    max_latency_ms DECIMAL(10,3),
    cluster_mode BOOLEAN,
    tls BOOLEAN,
    io_threads INTEGER,
    benchmark_threads INTEGER,
    benchmark_mode VARCHAR(50),
    duration INTEGER,
    warmup INTEGER,
    architecture VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Columns written by measured runs (see "Measured Runs" in README.md).
-- utils/push_to_postgres.py adds them with these types when it first sees
-- them (utils/measured_fields.py FIELD_TYPES); they are listed here so a fresh
-- database has them from the start.
ALTER TABLE benchmark_metrics
    ADD COLUMN IF NOT EXISTS generator TEXT,
    ADD COLUMN IF NOT EXISTS generator_version TEXT,
    ADD COLUMN IF NOT EXISTS generator_argv TEXT,
    ADD COLUMN IF NOT EXISTS generator_errors INTEGER,
    ADD COLUMN IF NOT EXISTS generator_timeouts INTEGER,
    ADD COLUMN IF NOT EXISTS generator_conns_failed INTEGER,
    ADD COLUMN IF NOT EXISTS generator_samples INTEGER,
    ADD COLUMN IF NOT EXISTS generator_latency_scope TEXT,
    ADD COLUMN IF NOT EXISTS generator_cores DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p90_latency_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p999_latency_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p9999_latency_ms DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS server_ops_s DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS window_s DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS server_asserts INTEGER,
    ADD COLUMN IF NOT EXISTS connected_clients INTEGER,
    ADD COLUMN IF NOT EXISTS main_busy_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS main_busy_source TEXT,
    ADD COLUMN IF NOT EXISTS main_cpu_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS io_threads_cores DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS valid BOOLEAN,
    ADD COLUMN IF NOT EXISTS invalid_reasons TEXT,
    ADD COLUMN IF NOT EXISTS run_index INTEGER,
    ADD COLUMN IF NOT EXISTS rate INTEGER,
    ADD COLUMN IF NOT EXISTS thread_pinning TEXT,
    ADD COLUMN IF NOT EXISTS pinning_warnings TEXT,
    ADD COLUMN IF NOT EXISTS acl_user TEXT,
    ADD COLUMN IF NOT EXISTS perf_status TEXT,
    ADD COLUMN IF NOT EXISTS cyc_per_cmd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS ins_per_cmd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS ipc DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS l1i_miss_per_cmd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS l1d_miss_per_cmd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS br_miss_per_cmd DOUBLE PRECISION;

-- Create benchmark_commits table for tracking commit benchmarking status
CREATE TABLE IF NOT EXISTS benchmark_commits (
    id SERIAL PRIMARY KEY,
    sha VARCHAR(40) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('in_progress', 'complete')),
    config JSONB NOT NULL,
    architecture VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Unique constraint: same commit + config + architecture can only exist once
    CONSTRAINT unique_sha_config_arch UNIQUE(sha, config, architecture)
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_benchmark_metrics_unique 
    ON benchmark_metrics(timestamp, commit, command, data_size, pipeline, rps, cluster_mode, tls, io_threads, architecture);
CREATE INDEX IF NOT EXISTS idx_benchmark_metrics_timestamp_command ON benchmark_metrics(timestamp, command);
CREATE INDEX IF NOT EXISTS idx_commits_sha_status ON benchmark_commits(sha, status);
CREATE INDEX IF NOT EXISTS idx_commits_status ON benchmark_commits(status);
CREATE INDEX IF NOT EXISTS idx_commits_config ON benchmark_commits USING GIN(config);

-- Change ownership of tables and sequences to github_actions
ALTER TABLE IF EXISTS benchmark_metrics OWNER TO github_actions;
ALTER TABLE IF EXISTS benchmark_commits OWNER TO github_actions;
ALTER SEQUENCE IF EXISTS benchmark_metrics_id_seq OWNER TO github_actions;
ALTER SEQUENCE IF EXISTS benchmark_commits_id_seq OWNER TO github_actions;

-- Grant permissions to github_actions user for postgres database
GRANT CONNECT ON DATABASE postgres TO github_actions;
GRANT USAGE, CREATE ON SCHEMA public TO github_actions;
GRANT ALL PRIVILEGES ON benchmark_metrics TO github_actions;
GRANT ALL PRIVILEGES ON benchmark_commits TO github_actions;
GRANT ALL PRIVILEGES ON SEQUENCE benchmark_metrics_id_seq TO github_actions;
GRANT ALL PRIVILEGES ON SEQUENCE benchmark_commits_id_seq TO github_actions;

-- Grant permissions to postgres (admin) user for postgres database
GRANT ALL PRIVILEGES ON DATABASE postgres TO postgres;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO postgres;

-- Summary - show what was created
\dt
SELECT 'Database initialization complete!' as status;
SELECT 'Created databases: grafana (for Grafana), postgres (for benchmark data)' as summary;
SELECT 'Created tables: benchmark_metrics, benchmark_commits' as tables;
SELECT 'Created users: postgres (Admin with full access), github_actions (IAM-enabled)' as users;
