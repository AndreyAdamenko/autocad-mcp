"""Stream COM records into a separate Blender process via anonymous stdin pipe."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .extraction import RecordAssembler
from .interpretation import validate_rules


def discover_blender():
    configured = os.environ.get("ACAD_MCP_BLENDER")
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        raise ValueError("ACAD_MCP_BLENDER must name blender.exe")
    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
    paths = sorted(root.glob("Blender */blender.exe"), reverse=True)
    if not paths:
        raise RuntimeError("Blender executable not found; set ACAD_MCP_BLENDER")
    return paths[0]


def build(scan, target, rules=None, tolerance_mm=0.25, timeout=900):
    if scan.page != 0:
        raise ValueError("Blender build requires a fresh scan")
    if scan.manifest["mm_per_unit"] is None:
        raise ValueError("Drawing units are unresolved. Supply a sourced mm_per_unit override")
    if not 0.001 <= tolerance_mm <= 10:
        raise ValueError("tolerance_mm must be 0.001..10")
    rules = validate_rules(rules or [])
    target = Path(target).resolve()
    if target.suffix.lower() != ".blend" or target.exists():
        raise ValueError("target must be a new .blend file")
    target.parent.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("blender_worker.py")
    log_path = target.with_suffix(".blender.log")
    header = {"kind": "header", "manifest": scan.manifest, "rules": rules,
              "target": str(target), "tolerance_mm": tolerance_mm}
    assembler = RecordAssembler()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(discover_blender()), "--background", "--factory-startup",
                                    "--python-exit-code", "3", "--python", str(script)],
                                   stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, **options)
        def send(value):
            process.stdin.write((json.dumps(value, ensure_ascii=True, allow_nan=False)+"\n").encode("ascii"))
        try:
            send(header)
            while True:
                batch = scan.batch(scan.page, limit=250, max_bytes=256000)
                for record in assembler.accept(batch["fragments"]):
                    send({"kind": "record", "record": record})
                if batch["done"]:
                    if assembler.pending:
                        raise RuntimeError("Incomplete record fragments at end of scan")
                    send({"kind": "end", "scan": {k: v for k, v in batch.items() if k != "fragments"}})
                    break
            process.stdin.close()
            code = process.wait(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            raise
    report_path = target.with_suffix(".report.json")
    if code or not target.is_file() or not report_path.is_file():
        raise RuntimeError(f"Blender failed (exit {code}); see {log_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    verification = {k:v for k,v in report["verification"].items() if k not in ("checks", "failures", "missing_source_ids", "block_envelope_checks")}
    verification.update(check_count=len(report["verification"]["checks"]),
                        failure_count=len(report["verification"]["failures"]),
                        failure_samples=report["verification"]["failures"][:10],
                        block_envelope_check_count=len(report["verification"].get("block_envelope_checks", [])))
    return {"blend": str(target), "report": str(report_path), "log": str(log_path),
            "verification": verification, "source_unchanged": report["source_unchanged"],
            "counts": report["counts"], "renders": report.get("renders", [])}
