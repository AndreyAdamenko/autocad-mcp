"""Real bpy test with synthetic geometry. Does not test or emulate a live DWG."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.blender_bridge import discover_blender
from autocad_mcp.geometry import identity, insertion_matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    target = args.output.resolve()
    if target.exists():
        raise ValueError("Fixture output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"source_path":"synthetic fixture, not a DWG", "source_sha256":"fixture", "mm_per_unit":1,
                "readonly":True, "schema":"autocad-com-geometry/1"}
    rectangle = {"id":"Model/RECT", "type":"AcDbPolyline", "space":"Model", "status":"ok", "effective_layer":"Walls",
                 "world_matrix":identity(), "geometry":{"kind":"polyline", "closed":True,
                 "vertices":[[0,0,0],[4000,0,0],[4000,3000,0],[0,3000,0]], "bulges":[0,0,0,0]}}
    circle = {"id":"Model/INSERT/CIRCLE", "type":"AcDbCircle", "space":"Model", "status":"ok", "effective_layer":"Other",
              "world_matrix":insertion_matrix([6000,2000,0],[0,0,1],0.4,[-2,3,1],[0,0,0]),
              "geometry":{"kind":"circle", "center":[0,0,0], "normal":[0,0,1], "radius":500}}
    rule = {"source_id":"Model/RECT", "category":"Walls", "confirmed":True, "evidence":"synthetic test specification",
            "height_mm":{"value":2800,"confirmed":True,"source":"fixture"},
            "base_z_mm":{"value":0,"confirmed":True,"source":"fixture"}}
    messages = [{"kind":"header", "manifest":manifest, "rules":[rule], "target":str(target), "tolerance_mm":0.25},
                {"kind":"record", "record":rectangle}, {"kind":"record", "record":circle},
                {"kind":"end", "scan":{"done":True,"source_unchanged":True,"source_sha256_after":"fixture", "counts":{"records":2}}}]
    worker = Path(__file__).resolve().parents[1]/"autocad_mcp"/"blender_worker.py"
    with target.with_suffix(".blender.log").open("wb") as log:
        result = subprocess.run([str(discover_blender()), "--background", "--factory-startup", "--python-exit-code","3","--python",str(worker)],
                                input="".join(json.dumps(m)+"\n" for m in messages).encode(), stdout=log, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode:
        raise RuntimeError(f"bpy fixture failed: {target.with_suffix('.blender.log')}")
    report = json.loads(target.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["counts"].get("solids") == 1, report["counts"]
    assert report["verification"]["coordinate_checks_passed"], report["verification"]["failures"]
    assert report["verification"]["full_fidelity"], report["issues"]
    print(json.dumps({"fixture":"synthetic", "blend":str(target), "counts":report["counts"], "verification_passed":True}))


if __name__ == "__main__":
    main()
