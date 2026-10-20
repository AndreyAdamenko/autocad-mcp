"""Check new extraction signatures against the installed target type library."""
import json
import sys
from pathlib import Path

import pythoncom
from acax_probe import find_library, each_type

EXPECTED = {
    'IAcadLine': ['StartPoint', 'EndPoint', 'Length', 'Thickness', 'Normal'],
    "IAcadMLeader": ["TextString", "LeaderCount", "GetLeaderIndex", "GetLeaderLineIndexes", "GetLeaderLineVertices"],
    "IAcadMLine": ["Coordinates", "StyleName", "MLineScale", "Justification"],
    "IAcadDocuments": ["Open", "Item", "Count"],
    "IAcadDocument": ["GetVariable", "HandleToObject", "ReadOnly", "ModelSpace", "Layouts", "Blocks"],
    "IAcadLWPolyline": ["Coordinates", "Normal", "Elevation", "Closed", "GetBulge", "GetWidth"],
    "IAcadPolyline": ["Coordinates", "Type", "Normal", "Elevation", "GetBulge"],
    "IAcad3DPolyline": ["Coordinates", "Closed"],
    "IAcadBlockReference": ["Name", "EffectiveName", "InsertionPoint", "Normal", "Rotation", "XScaleFactor", "YScaleFactor", "ZScaleFactor", "GetAttributes", "GetConstantAttributes", "InsUnitsFactor"],
    "IAcadBlock": ["Origin", "Units", "IsXRef", "Count", "Item"],
    "IAcadArc": ["Center", "Radius", "Normal", "StartPoint", "EndPoint", "StartAngle", "EndAngle", "ArcLength"],
    "IAcadEllipse": ["Center", "MajorAxis", "Normal", "RadiusRatio", "StartParameter", "EndParameter"],
    "IAcadSpline": ["ControlPoints", "Knots", "Weights", "IsRational", "Degree", "Closed", "IsPeriodic"],
    "IAcadEntity": ["GetBoundingBox"],
    "IAcadLayout": ["Block", "Name"],
}


def main():
    path = find_library()
    lib = pythoncom.LoadTypeLib(str(path))
    found = {}
    for name, ti in each_type(lib):
        if name not in EXPECTED:
            continue
        members = {}
        attr = ti.GetTypeAttr()
        for i in range(attr.cFuncs):
            fd = ti.GetFuncDesc(i)
            member = ti.GetNames(fd.memid)[0]
            if member in EXPECTED[name]:
                members.setdefault(member, []).append({"invoke_kind": fd.invkind, "parameter_flags": [p[1] for p in fd.args]})
        found[name] = members
    missing = [f"{name}.{member}" for name,wanted in EXPECTED.items() for member in wanted if member not in found.get(name,{})]
    report = {"typelib":str(path), "members":found, "missing":missing,
              "note":"Reflection checks availability/signatures, not live runtime acceptance"}
    output = Path(sys.argv[1]) if len(sys.argv)>1 else Path("evidence/extraction_api.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"typelib":str(path), "interfaces":len(found), "missing":missing}))
    return bool(missing)


if __name__ == "__main__":
    raise SystemExit(main())
