# Valkey Benchmark Dashboard Infrastructure

AWS infrastructure for visualizing Valkey performance benchmarks using Grafana, EKS Fargate, CloudFront CDN, and RDS PostgreSQL.

For a view that needs no server or database, see [Static dashboard](#static-dashboard).

## Static dashboard

`utils/build_static_dashboard.py` turns benchmark results into a static site
that charts them in the browser. It reads every `metrics.json` under a results
directory, flattens the entries into the same columns as the
`benchmark_metrics` table (the flattening is shared with `push_to_postgres.py`
in `utils/metric_rows.py`), and writes `data.json` and `data.csv` next to a
copy of [`static/`](static/).

```bash
python utils/build_static_dashboard.py --results-dir results --out site
python utils/build_static_dashboard.py --results-dir results --out site --inline
```

`--inline` also writes the data, script, style and chart library into
`site/index.html`, so that single file opens from `file://` or in a sandboxed
viewer. Other options: `--title`, `--set-field KEY=VALUE` (adds a column such as
`branch=unstable` to rows that lack it), `--test-type` (as in
`push_to_postgres.py`) and `--view QUERY` (the view the page opens with).

Beyond the table columns, each row gets:

| Field | Meaning |
|---|---|
| `parent.child` | One column per leaf of a nested object, e.g. `config_set.io-threads`; the object itself stays as JSON text, as in the JSONB column |
| `config_hash` | 12-hex hash of the row's configuration columns; equal configs share it across commits, branches and runs |
| `run_index` | The row's `run` field if present, else its position among rows with the same commit and `config_hash` (1-based, file order) |

Numeric columns named `rps`, `server_ops_s*`, `*_ms`, `*_us`, `*_bytes`,
`*_per_key`, `*_ops_s` or `populate_*` are measurements; everything else is a
dimension. Identity and result fields (`commit`, `timestamp`, `branch`,
`build`, `status`, `env_*`, `window_*`, measurements) stay out of `config_hash`.

### What the page does

- **Chart**: pick the metric (ops/s, server ops/s, p50/p99/p99.9 latency,
  memory, any other measurement) and the X axis (any dimension: `clients`,
  `pipeline`, `data_size`, `io_threads`, `commit`, `timestamp`, scenario fields,
  or `config`, a readable label of the configuration). Colour by any dimension
  (commit, branch, config, scenario). Runs are aggregated by median, mean, min
  or max; "Show each run" adds the individual runs.
- **Compare**: pick a column (commit, branch, build) and two of its values.
  The page matches rows by `config_hash` and shows both absolute values and the
  Δ % per config, with a ±2% band (adjustable) shaded.
- **Table**: the filtered rows, sortable, with CSV download. The chart and
  compare views also have a table of the numbers they plot.
- **Filters** on any column: value sets for dimensions, min/max ranges for
  measurements.
- The view lives in the URL query string, so a link reproduces it.
- Light and dark follow the system setting. The chart library, Apache ECharts
  6.1.0, is vendored in [`static/vendor/echarts/`](static/vendor/echarts/)
  (715 KB, 240 KB gzipped), so the page loads nothing from the network except
  its own `data.json`.

### Viewing results

- **Locally**: build the site, then open `site/index.html` directly if it was
  built with `--inline`, or serve the folder on loopback:
  `python3 -m http.server 8000 --bind 127.0.0.1 --directory site` and open
  `http://127.0.0.1:8000/`. Browsers refuse `fetch()` from `file://`, so a
  non-inline page needs the server.
- **From a workflow run**: `valkey_benchmark.yml`, `search_benchmark.yml` and
  `static_dashboard.yml` upload the site as the `dashboard-site` artifact
  (built with `--inline`) through the composite action
  [`.github/actions/static-dashboard`](../.github/actions/static-dashboard/action.yml).
  Download it with `gh run download <run-id> -n dashboard-site -D site`, then
  open `site/index.html`. `search_benchmark.yml` checks out this repository's
  `main` branch, so its step works once the action is merged there.
- **On GitHub Pages**: `static_dashboard.yml` has a `deploy-pages` job that is
  off by default. It runs only when the run's `deploy_pages` input is true and
  the repository variable `STATIC_DASHBOARD_PAGES` is `true`, and it needs
  Pages enabled with "GitHub Actions" as the source. A Pages site of a public
  repository is public, so deploy only results you are willing to publish.

`static_dashboard.yml` runs `benchmark.py` with `configs/dashboard-smoke.json`
(SET and GET at pipeline 1 and 8, two runs) on `ubuntu-latest` by default, so a
fork can try the whole path in a few minutes. Its inputs select another
runner, config, commits and run count.

## Overview

This infrastructure provides a serverless, scalable platform for benchmark visualization:

- **AWS CloudFront** - Global CDN for low-latency dashboard access
- **Application Load Balancer** - Kubernetes ingress with automatic provisioning
- **Amazon EKS Fargate** - Serverless Kubernetes (no EC2 nodes to manage)
- **Grafana** - Visualization platform with public dashboard sharing
- **Amazon RDS PostgreSQL 17** - Dual-database setup with IAM authentication
- **VPC with 2 AZs** - High availability networking

## Architecture

```
Internet Users
     ↓ HTTPS
CloudFront CDN (Global Edge Locations)
     ↓ HTTP
Application Load Balancer (CloudFront-only access)
     ↓ HTTP:3000
EKS Fargate Cluster (Serverless Kubernetes)
  ├─ Grafana Pods
  └─ AWS Load Balancer Controller
     ↓ PostgreSQL
RDS PostgreSQL 17 (Private Subnets, Encrypted)
```

### Infrastructure Components

| Component | Specification | Purpose |
|-----------|--------------|---------|
| **VPC** | 10.0.0.0/16, 2 AZs | Network isolation |
| **Public Subnets** | 10.0.1.0/24, 10.0.2.0/24 | ALB, NAT Gateways |
| **Private Subnets** | 10.0.10.0/24, 10.0.11.0/24 | Fargate pods, RDS |
| **EKS Fargate** | Kubernetes 1.33 | Serverless container platform |
| **RDS PostgreSQL** | 17, db.t4g.micro, Multi-AZ | Database |
| **CloudFront** | HTTPS, Global CDN | Content delivery |
| **ALB** | Application Load Balancer | Ingress controller |

### Database Configuration

Two databases on a single RDS instance:

1. **`grafana`** - Grafana configuration and dashboards
2. **`postgres`** - Benchmark metrics data
   - `benchmark_metrics` table - Performance data
   - `benchmark_commits` table - Commit tracking (used by `postgres_track_commits.py`)

Two IAM-enabled users:

1. **`postgres`** - Admin with full access
2. **`github_actions`** - CI/CD user with IAM-only authentication

### Grafana Dashboards

Dashboard definitions live in `grafana/` and are provisioned into Grafana:

| File | Purpose |
|------|---------|
| `valkey-throughput-comparison.json` | Throughput (RPS) comparison across commits |
| `valkey-performance-per-command.json` | Per-command latency/throughput breakdown |
| `valkey-module-perf.json` | Module (e.g. valkey-search) performance metrics |
| `grafana-values.yaml` | Helm values for the Grafana deployment |

Changes to `grafana/*.json` on `main` are automatically synced to the running
Grafana instance by the `sync-dashboards.yml` workflow (also manually
dispatchable).

## Prerequisites

- **AWS CLI** - Configured with credentials
- **kubectl** - Kubernetes command-line tool
- **helm** - Kubernetes package manager (v3+)
- **jq** - JSON processor
- **AWS Account** - Permissions for VPC, EKS, RDS, CloudFront, IAM

## Quick Start

```bash
cd dashboards/scripts
chmod +x *.sh

./00-check-prerequisites.sh      # Validate tools and credentials
./01-deploy-infrastructure.sh    # Deploy AWS infrastructure
./02-setup-kubernetes.sh         # Setup Kubernetes
./03-deploy-grafana.sh           # Deploy Grafana
./04-setup-database.sh           # Initialize databases
./05-setup-cloudfront.sh         # Setup CloudFront CDN
./06-finalize-deployment.sh      # Finalize and display summary
```

**You'll be prompted for:**
- RDS master password (Phase 1)
- Database setup confirmation (Phase 4)

## Deployment Phases

| Phase | Script | Purpose |
|-------|--------|---------|
| 0 | `00-check-prerequisites.sh` | Validate tools and AWS credentials |
| 1 | `01-deploy-infrastructure.sh` | Deploy VPC, EKS, RDS via CloudFormation |
| 2 | `02-setup-kubernetes.sh` | Configure kubectl and Load Balancer Controller |
| 3 | `03-deploy-grafana.sh` | Deploy Grafana with ALB Ingress |
| 4 | `04-setup-database.sh` | Initialize PostgreSQL databases |
| 5 | `05-setup-cloudfront.sh` | Add CloudFront CDN and secure ALB |
| 6 | `06-finalize-deployment.sh` | Finalize and display summary |

### What Gets Deployed

**Phase 1 - Infrastructure:**
- VPC with public/private subnets (2 AZs)
- NAT Gateways (2)
- EKS Fargate cluster with profiles
- RDS PostgreSQL (Multi-AZ, encrypted)
- Security groups and IAM roles

**Phase 2 - Kubernetes:**
- kubectl configuration
- AWS Load Balancer Controller
- Grafana namespace

**Phase 3 - Grafana:**
- Grafana deployment (Helm)
- ALB Ingress
- Application Load Balancer (automatic)

**Phase 4 - Database:**
- Two databases (grafana, postgres)
- Two users (postgres, github_actions)
- Tables and indexes

**Phase 5 - CloudFront:**
- CloudFront distribution
- ALB security hardening (CloudFront-only access)
- Grafana configuration update

**Phase 6 - Finalization:**
- Disable public EKS access
- Display deployment summary

### Configuration Files

After deployment:
- `deployment-config.env` - All configuration values
- `stack-outputs.json` - CloudFormation outputs
- `alb-dns-name.txt` - ALB DNS name

### Public Access (Dashboards)
```
URL: https:dashboards.valkey-io/public-dashboards/*
```

## Cleanup

```bash
# Load configuration
source deployment-config.env

# Delete CloudFormation stack
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"

# Wait for deletion
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"

# Clean up local files
rm -f deployment-config.env stack-outputs.json alb-dns-name.txt grafana-values-updated.yaml
```

## References

- [AWS EKS Documentation](https://docs.aws.amazon.com/eks/)
- [Grafana Documentation](https://grafana.com/docs/)
- [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/)
- [CloudFormation Reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/)

## Support

For issues please open an issue in this repository at https://github.com/valkey-io/valkey-perf-benchmark/issues
