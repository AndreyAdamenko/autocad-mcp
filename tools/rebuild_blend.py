"""Rebuild a final BLEND from its embedded COM provenance after an adapter fix.

No DWG processing/export or external geometry file. Reuses actual source COM
records embedded by a completed live scan, then runs the same bpy pipeline.
"""
import base64
import io
import json
import sys
import zlib
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.blender_worker import main as build

def decode(name):
    if name in bpy.data.texts:
        return bpy.data.texts[name].as_string()
    return zlib.decompress(base64.b85decode(bpy.data.texts[name+".zlib.base85"].as_string())).decode()

lines = decode("AutoCAD source provenance.jsonl").splitlines()
manifest = json.loads(lines[0])["manifest"]
records = [json.loads(line) for line in lines[1:] if line]
old = json.loads(decode("AutoCAD verification report.json"))
target = Path(sys.argv[sys.argv.index("--")+1]).resolve()
if target.exists() or target.suffix != ".blend":
    raise ValueError("Rebuild requires a new .blend path")
messages = [{"kind":"header","manifest":manifest,"rules":old["analysis"]["rules"],
             "target":str(target),"tolerance_mm":old["verification"]["tolerance_mm"]}]
messages += [{"kind":"record","record":r} for r in records]
messages.append({"kind":"end","scan":{"done":True,"source_unchanged":old["source_unchanged"],
                 "source_sha256_after":old["source_sha256_after"],"counts":{"records":len(records)}}})
payload = "".join(json.dumps(m,ensure_ascii=True)+"\n" for m in messages).encode()
bpy.ops.wm.read_factory_settings(use_empty=False)
sys.stdin = io.TextIOWrapper(io.BytesIO(payload),encoding="utf-8")
build()
