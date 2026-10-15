"""Reopen-test script, run by Blender with an existing final .blend.

blender --background final.blend --python-exit-code 3 --python tools/verify_blend.py
Recalculates evaluated vertices after loading the saved file and checks the DWG
hash. Reads provenance from the final BLEND itself, no exchange geometry files.
"""
import hashlib
import base64
import zlib
import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.blender_worker import verify
from autocad_mcp.geometry import world_geometry


def main():
    target = Path(bpy.data.filepath)
    def embedded_text(name):
        if name in bpy.data.texts:
            return bpy.data.texts[name].as_string()
        encoded = bpy.data.texts[name+".zlib.base85"].as_string()
        return zlib.decompress(base64.b85decode(encoded)).decode("utf-8")
    lines = embedded_text("AutoCAD source provenance.jsonl").splitlines()
    manifest = json.loads(lines[0])["manifest"]
    records = [json.loads(line) for line in lines[1:] if line]
    by_id = {r["id"]: r for r in records}
    stored_report = json.loads(embedded_text("AutoCAD verification report.json"))
    objects = []
    for obj in bpy.data.objects:
        source_id = obj.get("source_id")
        if source_id not in by_id or obj.type != "MESH":
            continue
        record = by_id[source_id]
        pts,_,_,_ = world_geometry(record, stored_report["verification"]["tolerance_mm"])
        if "height_mm" in obj:
            rule = json.loads(obj["rule_evidence"])
            base, height = rule["base_z_mm"]["value"], rule["height_mm"]["value"]
            expected = [[p[0],p[1],base] for p in pts]+[[p[0],p[1],base+height] for p in pts]
            role = "solid"
        else:
            expected, role = pts, "reference"
        objects.append((obj,expected,record,role))
    report = verify(objects, records, manifest, stored_report["issues"], stored_report["verification"]["tolerance_mm"])
    source = Path(manifest["source_path"])
    if source.is_file():
        h = hashlib.sha256()
        with source.open("rb") as stream:
            for part in iter(lambda: stream.read(1024*1024), b""):
                h.update(part)
        source_unchanged = h.hexdigest() == manifest["source_sha256"]
    else:
        source_unchanged = None
    summary = {k:v for k,v in report.items() if k not in ("checks", "failures", "block_envelope_checks")}
    summary.update(blend=str(target), reopened=True, blender_version=bpy.app.version_string,
                   source_unchanged=source_unchanged, check_count=len(report["checks"]),
                   failure_count=len(report["failures"]), failure_samples=report["failures"][:20],
                   block_envelope_check_count=len(report.get("block_envelope_checks", [])))
    target.with_suffix(".reopen-verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True))
    if not report["coordinate_checks_passed"] or source_unchanged is False:
        raise RuntimeError("Reopened model failed geometry/source verification")


if __name__ == "__main__":
    main()
