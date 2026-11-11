# Data Incident Lab

A reproducible portfolio project about **detecting and recovering from bad data deliveries**. A fictional shop produces seven daily partitions of orders, payments, and refunds. The lab corrupts one delivery, records quality incidents, restores the source, reruns the pipeline, and reconciles final revenue with an independent truth fixture.

## Run it

Python 3.10+; no runtime dependencies:

```bash
python incident_lab.py demo --dir demo
```

Or with Docker Compose:

```bash
docker compose up --build
```

Docker Compose requires a running Docker daemon. The generated files are in `demo/`:

| File | Purpose |
| --- | --- |
| `broken_report.html` | Incident view before recovery |
| `recovered_report.html` | Final status and daily revenue |
| `broken_result.json`, `recovered_result.json` | Machine readable evidence |
| `revenue_daily.csv` | Final analytical mart |
| `lab.db` | SQLite raw events, mart, current incidents, and run history |
| `clean/`, `broken/` | Canonical and recovered daily source partitions |
| `truth.jsonl` | Independent fixture used only for the final reconciliation |

Open both HTML reports locally to see the before and after state. `demo` prints a compact JSON summary; the expected outcome is `failed` followed by `passed`, with `reconciled: true` and `net_cents: 252000`.

## What breaks

- Eight repeated event IDs in one partition. Identical duplicates are suppressed and reported as warnings; conflicting duplicates fail the run.
- An order amount changes from an integer to a string. The invalid row is rejected, and its payment becomes orphaned.
- One daily partition is missing.
- One refund has no matching order.
- Four payments arrive later than their business date. They appear as informational incidents and are attributed to the **business date** in the mart.

The pipeline validates each JSONL record, deduplicates on `event_id`, checks order/payment/refund relationships, and writes a seven-day revenue mart. The source files remain separate from the database. A repeat run of the same source produces the same rows and source hash, while appending a new audit entry to `runs`.

## Run against your own source

Supply one file per business date, named `YYYY-MM-DD.jsonl`, for 2026-01-05 through 2026-01-11. Each line needs `event_id`, `kind` (`order`, `payment`, or `refund`), `order_id`, positive integer `amount_cents`, `occurred_on`, and `arrived_on`.

```bash
python incident_lab.py run --source path/to/partitions --database lab.db --report report.html
```

The command exits with code `2` when an error-level incident is present, so it can fail a scheduled job. It exits with `0` for a clean run, even if informational late arrivals are present.

## Verify

```bash
python -m unittest discover -s tests -v
```

The tests cover detection, recovery, exact revenue reconciliation, and idempotent repeat execution.

## Architecture and scope

```text
JSONL daily partitions
       │
       ▼
parse → schema validation → event-ID deduplication
       │                         │
       ▼                         ▼
relationship checks         incidents
       │
       ▼
SQLite raw_events → revenue_daily → HTML / CSV
       │
       └── runs (timestamp, source hash, status)
```

This is a local, deterministic batch MVP. Each run rebuilds the current mart from its source partitions in one SQLite transaction; it does not yet use incremental processing, Airflow, dbt, PostgreSQL, or Kafka. The next production-oriented iteration would add incremental partition replacement, scheduled orchestration, formal data contracts, and external storage. The current design makes the failure and recovery behavior reviewable without cloud accounts or credentials.
