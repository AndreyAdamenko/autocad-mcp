"""Read a few problematic COM interfaces from the selected DWG, no mutations."""
import json
import sys
from pathlib import Path
import pythoncom
from win32com.client import VARIANT

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.acad_core import com_apartment, running_app
from autocad_mcp.com_read import open_source, method, read

source = Path(sys.argv[1])
report_path = Path(sys.argv[2])
inspection = json.loads(report_path.read_text(encoding="utf-8"))
report = {}
with com_apartment():
    doc = open_source(running_app(), source)
    for issue in inspection["issues"]:
        kind = issue["type"]
        if kind in report or kind not in ("AcDbSpline", "AcDbHatch", "AcDbMLeader", "AcDbTable", "AcDbViewport"):
            continue
        obj = method(doc, "HandleToObject", issue["id"].split("/")[-1])
        sample = {"handle": issue["id"]}
        try:
            sample["GetBoundingBox"] = obj.GetBoundingBox()
        except Exception as exc:
            sample["GetBoundingBox_error"] = str(exc)
        for prop in ("ControlPoints", "Knots", "Weights", "IsRational", "Degree", "Closed", "IsPeriodic",
                     "NumberOfLoops", "TextString", "TextLocation", "TextHeight", "LeaderCount", "ContentType",
                     "Rows", "Columns", "Center", "Width", "Height", "Direction", "Target", "CustomScale", "TwistAngle"):
            try:
                value = read(obj, prop)
                sample[prop] = list(value) if isinstance(value, tuple) else value
            except Exception as exc:
                sample[prop+"_error"] = str(exc)
        if kind == "AcDbHatch":
            try:
                loop = obj.GetLoopAt(0)
                sample["loop"] = [{"type":str(o.ObjectName), "handle":str(o.Handle)} for o in loop]
            except Exception as exc:
                sample["loop_error"] = str(exc)
            try:
                out = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
                obj.GetLoopAt(0, out)
                sample["loop_explicit_out"] = str(out.value)
            except Exception as exc:
                sample["loop_explicit_out_error"] = str(exc)
        report[kind] = sample
Path("evidence/geometry_interfaces.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
