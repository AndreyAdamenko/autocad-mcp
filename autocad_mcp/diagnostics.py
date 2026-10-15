"""Non-mutating live COM and installed type-library diagnostics."""
from __future__ import annotations

import argparse
import json
import platform
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pythoncom
import win32com.client

PROG_IDS = ("AutoCAD.Application.19.1", "AutoCAD.Application.19", "AutoCAD.Application")


def diagnose(prog_ids=PROG_IDS, expected_version="19.1", source=None):
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "architecture": platform.architecture()[0],
              "expected_version": expected_version, "attempts": [], "connected": False,
              "compatible_session": False, "drawing_read_test": False,
              "mutations_performed": False}
    pythoncom.CoInitialize()
    try:
        for prog_id in prog_ids:
            attempt = {"progid": prog_id}
            report["attempts"].append(attempt)
            try:
                app = win32com.client.GetActiveObject(prog_id)
                version = str(app.Version)
                attempt.update(connected=True, version=version, caption=str(app.Caption))
                report["connected"] = True
                if not re.match(re.escape(expected_version)+r"(?:\D|$)", version):
                    attempt["error"] = "Running application has a different version"
                    continue
                report.update(connected=True, compatible_session=True, version=version,
                              progid=prog_id, documents=int(app.Documents.Count))
                ti = app._oleobj_.GetTypeInfo()
                lib, _ = ti.GetContainingTypeLib()
                report["live_typelib"] = {"name": lib.GetDocumentation(-1)[0],
                                           "attributes": list(lib.GetLibAttr())}
                if source:
                    source = Path(source).resolve(strict=True)
                    if source.suffix.lower() != ".dwg":
                        raise ValueError("Only original DWGs are accepted")
                    before = hashlib.sha256(source.read_bytes()).hexdigest()
                    from .com_read import open_source
                    doc = open_source(app, source)
                    if not doc.ReadOnly:
                        raise RuntimeError("AutoCAD did not open the source read-only")
                    report["source_sha256_before"] = before
                elif not app.Documents.Count:
                    report["drawing_error"] = "No document is open"
                    break
                else:
                    doc = app.ActiveDocument
                report["drawing"] = {"name": str(doc.Name), "path": str(doc.FullName),
                                     "readonly": bool(doc.ReadOnly),
                                     "insunits": int(doc.GetVariable("INSUNITS")),
                                     "model_entities": int(doc.ModelSpace.Count),
                                     "layers": int(doc.Layers.Count),
                                     "blocks": int(doc.Blocks.Count)}
                samples = []
                for i in range(min(5, int(doc.ModelSpace.Count))):
                    obj = doc.ModelSpace.Item(i)
                    sample = {"handle": str(obj.Handle), "type": str(obj.ObjectName)}
                    for key in ("StartPoint", "EndPoint", "Coordinates", "Center", "TextString"):
                        try:
                            value = getattr(obj, key)
                            sample[key] = list(value) if isinstance(value, tuple) else value
                        except Exception:
                            pass
                    samples.append(sample)
                report["samples"] = samples
                if source:
                    after = hashlib.sha256(source.read_bytes()).hexdigest()
                    report.update(source_sha256_after=after, source_unchanged=before == after)
                report["drawing_read_test"] = True
                break
            except Exception as exc:
                attempt.setdefault("connected", False)
                attempt.update(error=str(exc), hresult=getattr(exc, "hresult", None))
    finally:
        pythoncom.CoUninitialize()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path, help="Open this DWG with ReadOnly=True")
    args = parser.parse_args()
    report = diagnose(source=args.source)
    value = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(value, encoding="utf-8")
    print(value)
    return 0 if report["drawing_read_test"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
