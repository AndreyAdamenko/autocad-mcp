"""CLI for live read-only inspection or direct COM -> bpy streaming."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .acad_core import com_apartment, running_app
from .com_read import open_source
from .extraction import GeometryScan, RecordAssembler


def inspect(scan):
    from .interpretation import analyse
    assembler, records, byte_sizes = RecordAssembler(), [], []
    while True:
        batch = scan.batch(scan.page, limit=250, max_bytes=256000)
        byte_sizes.append(len(json.dumps(batch, ensure_ascii=True).encode("utf-8")))
        records.extend(assembler.accept(batch["fragments"]))
        print(f"batch={batch['page']} records={len(records)} done={batch['done']}", flush=True)
        if batch["done"]:
            break
    if assembler.pending:
        raise RuntimeError("Unfinished geometry fragments")
    issues = [{"id": r["id"], "type": r["type"], "error": r.get("error") or r["geometry"].get("reason")}
              for r in records if r["status"] != "ok"]
    return {"manifest": scan.manifest, "counts": batch["counts"], "batches": len(byte_sizes),
            "max_batch_bytes": max(byte_sizes), "types": dict(Counter(r["type"] for r in records)),
            "issues": issues, "source_unchanged": batch["source_unchanged"],
            "source_sha256_after": batch["source_sha256_after"], "analysis": analyse(records)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="New final .blend (geometry is never written to an exchange file)")
    parser.add_argument("--inspect-report", type=Path)
    parser.add_argument("--scope", choices=("model", "all"), default="all")
    parser.add_argument("--rules", type=Path, help="JSON rule list, confirmed parameters only")
    parser.add_argument("--mm-per-unit", type=float)
    parser.add_argument("--units-evidence")
    args = parser.parse_args()
    if not args.output and not args.inspect_report:
        parser.error("--output or --inspect-report is required")
    with com_apartment():
        doc = open_source(running_app(), args.source)
        scan = GeometryScan(doc, args.scope, args.mm_per_unit, args.units_evidence)
        if args.output:
            from .blender_bridge import build
            rules = json.loads(args.rules.read_text(encoding="utf-8")) if args.rules else []
            outcome = build(scan, args.output, rules)
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
        else:
            report = inspect(scan)
            args.inspect_report.parent.mkdir(parents=True, exist_ok=True)
            args.inspect_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            print(json.dumps({k:v for k,v in report.items() if k not in ("analysis", "issues", "manifest")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
