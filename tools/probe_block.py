"""Independent live read-only check of one insert and its definition geometry."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.acad_core import com_apartment, running_app
from autocad_mcp.com_read import open_source, method
from autocad_mcp.extraction import iter_records
from autocad_mcp.geometry import bounds, world_geometry, transform
from itertools import product

class One:
    Count = 1
    def __init__(self, obj):
        self.obj = obj
    def Item(self, _):
        return self.obj

with com_apartment():
    doc = open_source(running_app(), sys.argv[1])
    obj = method(doc, "HandleToObject", sys.argv[2])
    records = list(iter_records(doc, [("Probe", One(obj))], 1))
    for record in records:
        try:
            pts,*_ = world_geometry(record)
            record["calculated_world_bbox"] = bounds(pts)
        except Exception:
            pass
    if len(records)>1:
        definition_box = bounds([p for r in records[1:] if r.get("source_bbox") for p in r["source_bbox"]])
        box_corners = list(product(*zip(*definition_box)))
        summary = {"block":records[0], "geometry_bbox":bounds([p for r in records[1:] if "calculated_world_bbox" in r for p in r["calculated_world_bbox"]]),
                   "transformed_definition_box":bounds([transform(records[1]["world_matrix"], p) for p in box_corners])}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(records, ensure_ascii=False, indent=2))
