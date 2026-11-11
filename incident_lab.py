"""Reproducible batch-data incident lab. No third-party runtime dependencies."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

START = date(2026, 1, 5)
DAYS = 7
ORDERS_PER_DAY = 20
REQUIRED = {"event_id", "kind", "order_id", "amount_cents", "occurred_on", "arrived_on"}
KINDS = {"order", "payment", "refund"}


def days() -> list[str]:
    return [(START + timedelta(days=i)).isoformat() for i in range(DAYS)]


def event(event_id: str, kind: str, order_id: str, amount: int, occurred: str, arrived: str | None = None) -> dict:
    return {"event_id": event_id, "kind": kind, "order_id": order_id,
            "amount_cents": amount, "occurred_on": occurred, "arrived_on": arrived or occurred}


def make_fixture(root: Path) -> None:
    """Create a deterministic truth set and a deliberately broken delivery."""
    if root.exists():
        shutil.rmtree(root)
    clean = root / "clean"
    broken = root / "broken"
    clean.mkdir(parents=True)
    broken.mkdir()
    truth = []
    for i, day in enumerate(days()):
        batch = []
        for j in range(ORDERS_PER_DAY):
            oid = f"O{i:02d}{j:03d}"
            amount = 1000 + 100 * j
            batch.append(event(f"order-{oid}", "order", oid, amount, day))
            arrival = days()[4] if i == 1 and j < 4 else day
            batch.append(event(f"payment-{oid}", "payment", oid, amount, day, arrival))
            if j % 10 == 0:
                batch.append(event(f"refund-{oid}", "refund", oid, amount, day))
        truth.extend(batch)
        write_jsonl(clean / f"{day}.jsonl", batch)
        damaged = [dict(row) for row in batch]
        if i == 2:
            damaged.extend(dict(row) for row in batch[:8])
        if i == 3:
            damaged[0]["amount_cents"] = "unknown"
        if i == 4:
            damaged.append(event("refund-orphan", "refund", "O99999", 900, day))
        if i != 5:
            write_jsonl(broken / f"{day}.jsonl", damaged)
    write_jsonl(root / "truth.jsonl", truth)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS raw_events (
      event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, order_id TEXT NOT NULL,
      amount_cents INTEGER NOT NULL, occurred_on TEXT NOT NULL,
      arrived_on TEXT NOT NULL, source_file TEXT NOT NULL, payload_hash TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS revenue_daily (
      day TEXT PRIMARY KEY, orders INTEGER NOT NULL, paid_cents INTEGER NOT NULL,
      refunded_cents INTEGER NOT NULL, net_cents INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS incidents (
      code TEXT NOT NULL, severity TEXT NOT NULL, subject TEXT NOT NULL,
      detail TEXT NOT NULL, PRIMARY KEY (code, subject)
    );
    CREATE TABLE IF NOT EXISTS runs (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
      source_hash TEXT NOT NULL, status TEXT NOT NULL, incident_count INTEGER NOT NULL
    );
    """)
    return db


def valid(row: object) -> bool:
    if not isinstance(row, dict) or not REQUIRED.issubset(row):
        return False
    if not all(isinstance(row[k], str) and row[k] for k in
               ("event_id", "kind", "order_id", "occurred_on", "arrived_on")):
        return False
    if row["kind"] not in KINDS or type(row["amount_cents"]) is not int or row["amount_cents"] <= 0:
        return False
    try:
        return (date.fromisoformat(row["occurred_on"]) in [date.fromisoformat(d) for d in days()]
                and date.fromisoformat(row["arrived_on"]) >= date.fromisoformat(row["occurred_on"]))
    except ValueError:
        return False


def add_issue(issues: list[dict], code: str, severity: str, subject: str, detail: str) -> None:
    issues.append({"code": code, "severity": severity, "subject": subject, "detail": detail})


def run(source: Path, database: Path, report: Path | None = None) -> dict:
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source}")
    issues: list[dict] = []
    seen: dict[str, dict] = {}
    hasher = hashlib.sha256()
    for day in days():
        path = source / f"{day}.jsonl"
        if not path.exists():
            add_issue(issues, "missing_partition", "error", day, "Expected daily file is absent")
            continue
        raw = path.read_bytes()
        hasher.update(day.encode() + raw)
        for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = None
            if not valid(row):
                add_issue(issues, "invalid_event", "error", f"{path.name}:{number}",
                          "Malformed JSON or invalid event schema")
                continue
            eid = row["event_id"]
            if eid in seen:
                code = "conflicting_event" if seen[eid] != row else "duplicate_event"
                severity = "error" if code == "conflicting_event" else "warning"
                add_issue(issues, code, severity, eid, "Repeated event ID in source")
                continue
            seen[eid] = row
    orders = {r["order_id"]: r for r in seen.values() if r["kind"] == "order"}
    payments = defaultdict(list)
    refunds = defaultdict(list)
    for row in seen.values():
        if row["kind"] == "payment":
            payments[row["order_id"]].append(row)
        elif row["kind"] == "refund":
            refunds[row["order_id"]].append(row)
        if row["arrived_on"] > row["occurred_on"]:
            add_issue(issues, "late_event", "info", row["event_id"],
                      f"Occurred {row['occurred_on']}; arrived {row['arrived_on']}")
    for oid, order in orders.items():
        paid = payments[oid]
        refunded = refunds[oid]
        if len(paid) != 1 or sum(r["amount_cents"] for r in paid) != order["amount_cents"]:
            add_issue(issues, "payment_mismatch", "error", oid,
                      "Expected one payment matching the order amount")
        if sum(r["amount_cents"] for r in refunded) > order["amount_cents"]:
            add_issue(issues, "refund_exceeds_order", "error", oid,
                      "Refund exceeds order amount")
    for oid in set(payments) | set(refunds):
        if oid not in orders:
            add_issue(issues, "orphan_event", "error", oid, "Payment or refund has no order")
    aggregates = {d: {"orders": 0, "paid_cents": 0, "refunded_cents": 0} for d in days()}
    for row in seen.values():
        bucket = aggregates[row["occurred_on"]]
        if row["kind"] == "order":
            bucket["orders"] += 1
        elif row["kind"] == "payment" and row["order_id"] in orders:
            bucket["paid_cents"] += row["amount_cents"]
        elif row["kind"] == "refund" and row["order_id"] in orders:
            bucket["refunded_cents"] += row["amount_cents"]
    issues.sort(key=lambda x: (x["severity"], x["code"], x["subject"]))
    status = "failed" if any(x["severity"] == "error" for x in issues) else "passed"
    db = connect(database)
    try:
        with db:
            db.execute("DELETE FROM raw_events")
            db.execute("DELETE FROM revenue_daily")
            db.execute("DELETE FROM incidents")
            for row in seen.values():
                payload = json.dumps(row, sort_keys=True)
                db.execute("INSERT INTO raw_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (row["event_id"], row["kind"], row["order_id"], row["amount_cents"],
                            row["occurred_on"], row["arrived_on"], f"{row['occurred_on']}.jsonl",
                            hashlib.sha256(payload.encode()).hexdigest()))
            for day, values in aggregates.items():
                db.execute("INSERT INTO revenue_daily VALUES (?, ?, ?, ?, ?)",
                           (day, values["orders"], values["paid_cents"], values["refunded_cents"],
                            values["paid_cents"] - values["refunded_cents"]))
            for item in issues:
                db.execute("INSERT OR IGNORE INTO incidents VALUES (?, ?, ?, ?)",
                           (item["code"], item["severity"], item["subject"], item["detail"]))
            db.execute("INSERT INTO runs (run_at, source_hash, status, incident_count) VALUES (?, ?, ?, ?)",
                       (datetime.now(timezone.utc).isoformat(), hasher.hexdigest(), status, len(issues)))
        daily = [dict(row) for row in db.execute("SELECT * FROM revenue_daily ORDER BY day")]
    finally:
        db.close()
    result = {"status": status, "source": str(source), "database": str(database),
              "events": len(seen), "incidents": issues, "daily": daily,
              "total_net_cents": sum(r["net_cents"] for r in daily),
              "source_hash": hasher.hexdigest()}
    if report:
        render_report(result, report)
    return result


def render_report(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    esc = lambda value: html.escape(str(value))
    rows = "".join("<tr>" + "".join(f"<td>{esc(row[k])}</td>" for k in
                   ("day", "orders", "paid_cents", "refunded_cents", "net_cents")) + "</tr>"
                   for row in result["daily"])
    incidents = "".join(f"<tr><td><span class='{esc(i['severity'])}'>{esc(i['severity'])}</span></td>"
                        f"<td>{esc(i['code'])}</td><td>{esc(i['subject'])}</td><td>{esc(i['detail'])}</td></tr>"
                        for i in result["incidents"])
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Data Incident Lab</title><style>
    body{{font:16px system-ui,sans-serif;max-width:1100px;margin:50px auto;padding:0 20px;color:#152235;background:#f5f7fa}}
    h1{{font-size:2.3rem}} .cards{{display:flex;gap:16px;flex-wrap:wrap}} .card{{background:white;padding:20px;border-radius:12px;min-width:180px;box-shadow:0 2px 10px #dce2ea}}
    .label{{color:#627086;font-size:.8rem;text-transform:uppercase;letter-spacing:.08em}} .value{{font-size:1.6rem;font-weight:700;margin-top:7px}}
    table{{border-collapse:collapse;width:100%;background:white;border-radius:12px;overflow:hidden}} th,td{{padding:10px 12px;border-bottom:1px solid #e5e9ef;text-align:left}}
    th{{background:#e9eef5}} .error{{color:#a02323;font-weight:700}} .warning{{color:#996300;font-weight:700}} .info{{color:#2161a0;font-weight:700}}
    h2{{margin-top:38px}} code{{overflow-wrap:anywhere}}
    </style><main><h1>Data Incident Lab</h1><p>Source: <code>{esc(result['source'])}</code></p>
    <div class="cards"><div class="card"><div class="label">Status</div><div class="value">{esc(result['status'].upper())}</div></div>
    <div class="card"><div class="label">Unique events</div><div class="value">{result['events']}</div></div>
    <div class="card"><div class="label">Incidents</div><div class="value">{len(result['incidents'])}</div></div>
    <div class="card"><div class="label">Net revenue</div><div class="value">${result['total_net_cents']/100:,.2f}</div></div></div>
    <h2>Daily revenue</h2><table><thead><tr><th>Day</th><th>Orders</th><th>Paid cents</th><th>Refunded cents</th><th>Net cents</th></tr></thead><tbody>{rows}</tbody></table>
    <h2>Incidents</h2><table><thead><tr><th>Severity</th><th>Code</th><th>Subject</th><th>Detail</th></tr></thead><tbody>{incidents or '<tr><td colspan="4">No incidents</td></tr>'}</tbody></table></main></html>"""
    path.write_text(page, encoding="utf-8")


def export_csv(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result["daily"][0]))
        writer.writeheader()
        writer.writerows(result["daily"])


def demo(root: Path) -> dict:
    make_fixture(root)
    broken = run(root / "broken", root / "lab.db", root / "broken_report.html")
    (root / "broken_result.json").write_text(json.dumps(broken, indent=2), encoding="utf-8")
    for file in (root / "broken").glob("*.jsonl"):
        file.unlink()
    for file in (root / "clean").glob("*.jsonl"):
        shutil.copy2(file, root / "broken" / file.name)
    recovered = run(root / "broken", root / "lab.db", root / "recovered_report.html")
    truth_rows = [json.loads(line) for line in (root / "truth.jsonl").read_text(encoding="utf-8").splitlines()]
    expected = sum(r["amount_cents"] * (1 if r["kind"] == "payment" else -1 if r["kind"] == "refund" else 0)
                   for r in truth_rows)
    recovered["expected_net_cents"] = expected
    recovered["reconciled"] = recovered["status"] == "passed" and recovered["total_net_cents"] == expected
    if broken["status"] != "failed" or not recovered["reconciled"]:
        raise RuntimeError("Demo did not reproduce failure and verified recovery")
    (root / "recovered_result.json").write_text(json.dumps(recovered, indent=2), encoding="utf-8")
    export_csv(recovered, root / "revenue_daily.csv")
    return {"broken_status": broken["status"], "broken_errors": sum(i["severity"] == "error" for i in broken["incidents"]),
            "recovered_status": recovered["status"], "reconciled": recovered["reconciled"],
            "net_cents": recovered["total_net_cents"], "output": str(root)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_demo = sub.add_parser("demo", help="Generate, break, detect, recover, and reconcile")
    p_demo.add_argument("--dir", type=Path, default=Path("demo"))
    p_run = sub.add_parser("run", help="Process existing JSONL daily partitions")
    p_run.add_argument("--source", type=Path, required=True)
    p_run.add_argument("--database", type=Path, default=Path("lab.db"))
    p_run.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = demo(args.dir) if args.command == "demo" else run(args.source, args.database, args.report)
    print(json.dumps(result, indent=2))
    if args.command == "run" and result["status"] == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
