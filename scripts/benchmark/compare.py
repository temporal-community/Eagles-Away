"""Compare two recovery benchmark runs (before vs after) as a Markdown report.

Reads `X.jsonl` and the `X.summary.json` written next to it by `recovery_benchmark.py`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

METRICS = {
    "completion_rate_initial": "completion rate (initial)",
    "completion_rate_after_one_tap": "completion after one recovery tap",
    "offers": "recovery offers",
    "dead_end_offers": "dead-end offers",
    "terminal_no_action": "terminal with no action",
    "t_first_option": "t_first_option s (median / p90)",
    "t_pair": "t_pair s (median / p90)",
    "t_decision": "t_decision s (median / p90)",
    "t_to_trip": "t_to_trip s (median / p90)",
    "next_step_s": "next_step s (median / p90)",
    "requests_initial": "SearchApi requests (initial runs)",
    "requests_total": "SearchApi requests (incl. follow-ups)",
}
GROUPS = ("all cases", "baseline (18)")


def load(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    summary = json.loads(path.with_suffix("").with_suffix(".summary.json").read_text())
    return rows, summary


def cell(summary: dict[str, Any], key: str) -> str:
    value = summary[key]
    if isinstance(value, dict):
        return f"{value['median']} / {value['p90']} (n={value['n']})"
    return str(value)


def seconds(value: float | None) -> str:
    return "—" if value is None else str(value)


def offers(row: dict[str, Any]) -> str:
    if not row["actions"]:
        return "none"
    return "; ".join(
        f"{a['kind']}{' ' + a['departure_city'] if a.get('departure_city') else ''} → "
        f"{a['follow_up'].get('outcome')} ({a['follow_up'].get('requests')} req)"
        for a in row["actions"]
    )


def flags(row: dict[str, Any]) -> str:
    notes = []
    if row["outcome"] == "complete" and row["requests"] == 0:
        notes.append("complete with 0 req")
    notes += [
        f"{a['kind']}{' ' + a['departure_city'] if a.get('departure_city') else ''} "
        "completes with 0 req"
        for a in row["actions"]
        if a["follow_up"].get("outcome") == "complete" and a["follow_up"].get("requests") == 0
    ]
    return "; ".join(notes)


def outcome(row: dict[str, Any]) -> str:
    text = row["outcome"]
    if row.get("preview"):
        text += f" ({row['preview']})"
    if row.get("recovered"):
        text += ", recovered"
    return text


def report(
    before: tuple[list[dict[str, Any]], dict[str, Any]],
    after: tuple[list[dict[str, Any]], dict[str, Any]],
    names: tuple[Path, Path],
) -> str:
    (before_rows, before_summary), (after_rows, after_summary) = before, after
    lines = ["# Recovery benchmark comparison (SIMULATED inventory, offline)", ""]
    for label, summary, path in (
        ("before", before_summary, names[0]),
        ("after", after_summary, names[1]),
    ):
        meta = summary["meta"]
        lines.append(
            f"- {label}: `{path}`, workflow `{meta['workflow']}`, "
            f"search cache {meta.get('cache', 'shared')}, concurrency {meta['concurrency']}, "
            f"wall time {meta['wall_seconds']} s, {meta['intercepted']} SearchApi requests, "
            f"{meta['unsupported']} unsupported request shapes"
        )
    groups = [g for g in GROUPS if g in before_summary["summary"] and g in after_summary["summary"]]
    lines += [
        "",
        "| metric | " + " | ".join(f"before: {g} | after: {g}" for g in groups) + " |",
        "|---|" + "---|---|" * len(groups),
        "| cases | "
        + " | ".join(
            f"{before_summary['summary'][g]['cases']} | {after_summary['summary'][g]['cases']}"
            for g in groups
        )
        + " |",
    ]
    for key, label in METRICS.items():
        lines.append(
            f"| {label} | "
            + " | ".join(
                f"{cell(before_summary['summary'][g], key)} | {cell(after_summary['summary'][g], key)}"
                for g in groups
            )
            + " |"
        )
    lines += [
        "",
        "Per case, `before → after`. Requests are initial + follow-ups. "
        "Cache flags (after) mark results that made no SearchApi request.",
        "",
        "| case | outcome | t_decision s | t_to_trip s | requests | offers before | offers after "
        "| after cache flags |",
        "|---|---|---|---|---|---|---|---|",
    ]
    previous = {r["id"]: r for r in before_rows}
    for row in after_rows:
        old = previous.get(row["id"])
        if old is None:
            continue
        reason = f" [{row['failure_reason']}]" if row.get("failure_reason") else ""
        lines.append(
            f"| {row['variant']}: {row['origin']} → {row['match']} "
            f"| {outcome(old)} → {outcome(row)}{reason} "
            f"| {seconds(old['t_decision'])} → {seconds(row['t_decision'])} "
            f"| {seconds(old['t_to_trip'])} → {seconds(row['t_to_trip'])} "
            f"| {old['requests']}+{old['total_requests'] - old['requests']} → "
            f"{row['requests']}+{row['total_requests'] - row['requests']} "
            f"| {offers(old)} | {offers(row)} | {flags(row)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--before", required=True, type=Path, help="before JSONL")
    parser.add_argument("--after", required=True, type=Path, help="after JSONL")
    parser.add_argument("--out", required=True, type=Path, help="Markdown output path")
    args = parser.parse_args()
    text = report(load(args.before), load(args.after), (args.before, args.after))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
