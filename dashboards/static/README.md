# Static benchmark dashboard

A single page that charts one results directory without PostgreSQL or Grafana.
It follows the per-second views of the data tiering benchmark dashboard: one tab
per test group, a checkbox per scenario, a metric sidebar, ops/s KPI cards,
per-second line charts, and client and server latency bars.

## Make the data

Run a config with `"per_second_sampling": true`, then export the results:

```bash
python benchmark.py --config configs/mixed-rw-smoke.json --valkey-path ../valkey
python utils/dashboard_export.py results/HEAD dashboards/static/data --title "my run"
```

`dashboard_export.py` writes, for every scenario run `<group>_<scenario>`:

| File | Content |
| --- | --- |
| `<name>.csv` | One row per second. The columns are the ones the data tiering `metrics-collector.sh` wrote, plus `elapsed_sec` |
| `<name>-get-latency.csv`, `<name>-set-latency.csv` | `valkey-benchmark --csv` latency of the GET and the SET process |
| `<name>-server-latency.txt` | `latency_percentiles_usec_<command>:p50=..,p90=..,p99=..,p99.9=..,p100=..` |
| `manifest.json` | Tabs, datasets and run metadata the page reads |

How the numbers are derived:

- Rates compare each sample with the previous one in the same run. The first
  row of a run has zero rates.
- `ops_per_sec` is the server's `instantaneous_ops_per_sec`. `total_commands_delta`
  is the change in `total_commands_processed` per second.
- `cpu_user` and `cpu_sys` are shares of every host CPU. `valkey_cpu_*` is the
  server main thread, 100 per busy core. `asio_cpu_pct` is every other server
  thread, whatever its name.
- Disk columns come from `/sys/block/<dev>/stat`, with 512 byte sectors and MB
  meaning MiB, as in the old collector. Disk and host CPU are recorded only when
  the server runs on the benchmark host.
- Tiering columns read the `data-tiering-unstable` `ext_storage_*` INFO names
  first and the prototype names second. Fields the server does not report are 0.
- Server latency is the difference between the first and last `LATENCY HISTOGRAM`
  readings of the run, so it covers only the measured phase. Its resolution is
  the histogram's power-of-two microsecond buckets.

## View it

The page loads `data/` next to itself, or the directory given by `?data=<url>`.
Browsers do not fetch files from `file://` pages, so serve it on loopback:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory dashboards/static
# open http://127.0.0.1:8000/view.html
```

The page loads Chart.js and PapaParse from cdn.jsdelivr.net.
