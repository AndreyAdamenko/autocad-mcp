"""MCP entry points for read-only CAD extraction and Blender reconstruction."""
from __future__ import annotations

import time
import uuid

from .acad_core import com_apartment, result, running_app, tool
from .com_read import open_source
from .diagnostics import diagnose
from .extraction import GeometryScan

SCANS = {}


@tool("diagnose_compatibility", "Read-only AutoCAD 2014 COM diagnostic; reports actual live version and property reads.")
def diagnose_compatibility(_):
    report = diagnose()
    return result(report["compatible_session"], "Actual COM diagnostics.", report=report)


@tool("open_source_drawing", "Open an original DWG through COM with ReadOnly=True; never converts or saves it.",
      {"path": {"type": "string"}}, ["path"])
def open_source_drawing(args):
    from .acad_core import document_info
    with com_apartment():
        doc = open_source(running_app(), args["path"])
        return result(True, "Source opened read-only.", drawing=document_info(doc))


@tool("start_geometry_scan", "Start read-only traversal of original DWG, including nested blocks and all layouts. Unknown units remain unresolved.",
      {"path": {"type": "string"}, "scope": {"type": "string", "enum": ["model", "all"]},
       "mm_per_unit": {"type": "number", "exclusiveMinimum": 0}, "units_evidence": {"type": "string"}}, ["path"])
def start_geometry_scan(args):
    # Expire abandoned scans; a bounded number of COM document references stay alive.
    for token, scan in list(SCANS.items()):
        if time.monotonic()-scan.created > 3600:
            del SCANS[token]
    if len(SCANS) >= 8:
        raise RuntimeError("Close an existing scan before starting another (maximum 8)")
    with com_apartment():
        doc = open_source(running_app(), args["path"])
        scan = GeometryScan(doc, args.get("scope", "all"), args.get("mm_per_unit"), args.get("units_evidence"))
        token = uuid.uuid4().hex
        SCANS[token] = scan
        return result(True, "Geometry scan started.", scan_id=token, next_page=0, manifest=scan.manifest)


@tool("read_geometry_batch", "Read bounded JSON fragments. Reassemble records by record_id and offset. A scan is complete only when done=true and source_unchanged=true.",
      {"scan_id": {"type": "string"}, "page": {"type": "integer", "minimum": 0},
       "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
       "max_bytes": {"type": "integer", "minimum": 4096, "maximum": 1000000}}, ["scan_id", "page"])
def read_geometry_batch(args):
    scan = SCANS.get(args["scan_id"])
    if scan is None:
        raise ValueError("Unknown or expired scan_id")
    with com_apartment():
        batch = scan.batch(args["page"], args.get("limit", 100), args.get("max_bytes", 256000))
    return result(True, "Geometry batch.", **batch)


@tool("close_geometry_scan", "Release a scan's COM references; leaves the read-only drawing open.",
      {"scan_id": {"type": "string"}}, ["scan_id"])
def close_geometry_scan(args):
    existed = SCANS.pop(args["scan_id"], None) is not None
    return result(True, "Scan released.", existed=existed)


@tool("interpret_geometry", "Classify a batch as unconfirmed architectural hypotheses and identify possible elevation labels. Does not assume heights.",
      {"records": {"type": "array", "items": {"type": "object"}, "maxItems": 1000}}, ["records"])
def interpret_geometry(args):
    from .interpretation import analyse
    records = args["records"]
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("records must be a list of at most 1000 items")
    return result(True, "Unconfirmed drawing interpretation.", analysis=analyse(records))


@tool("build_blender_model", "Stream an original read-only DWG through COM into bpy; save BLEND, renders and verification report. Unknown parameters are not filled. Does not overwrite outputs.",
      {"path": {"type": "string"}, "output": {"type": "string", "description": "New .blend relative to ACAD_MCP_OUTPUT_ROOT"},
       "scope": {"type": "string", "enum": ["model", "all"]}, "rules": {"type": "array", "items": {"type": "object"}},
       "tolerance_mm": {"type": "number", "minimum": 0.001, "maximum": 10},
       "mm_per_unit": {"type": "number", "exclusiveMinimum": 0}, "units_evidence": {"type": "string"},
       "root_handles": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10000},
       "visible_only": {"type": "boolean", "description": "With an explicit root selection, exclude invisible symbol variants and record exclusions"},
       "selection_evidence": {"type": "string"}}, ["path", "output"])
def build_blender_model(args):
    from .acad_core import output_path
    from .blender_bridge import build
    if 'root_handles' not in args and (args.get('visible_only') or 'selection_evidence' in args):
        raise ValueError('visible_only and selection_evidence require explicit root_handles')
    with com_apartment():
        target = output_path(args["output"], {".blend"})
        doc = open_source(running_app(), args["path"])
        scan = GeometryScan(doc, args.get("scope", "all"), args.get("mm_per_unit"), args.get("units_evidence"))
        if 'root_handles' in args:
            scan.select_root_handles(args['root_handles'], args.get('selection_evidence'), args.get('visible_only', False))
        outcome = build(scan, target, args.get("rules"), args.get("tolerance_mm", 0.25))
    return result(True, "Blender output created. Inspect verification and issues before accepting the model.", **outcome)


@tool('inspect_drawing_annotations','Read original DWG titles, text, dimensions and table cells through COM. Repeated block definitions catalogued once.',
      {'path':{'type':'string'},'max_items':{'type':'integer','minimum':1,'maximum':1000},
       'scope':{'type':'string','enum':['all','model','paper']}},['path'])
def inspect_drawing_annotations(args):
    from .catalogue import inspect_document
    limit=args.get('max_items',250)
    if type(limit) is not int or not 1<=limit<=1000:
        raise ValueError('max_items must be 1..1000')
    with com_apartment():
        report=inspect_document(open_source(running_app(),args['path']), scope=args.get('scope', 'all'))
    counts={key:len(report[key]) for key in ('texts','dimensions','tables')}
    for key in counts:
        report[key]=report[key][:limit]
    return result(True,'Drawing annotations and schedules.',report=report,counts=counts,
                  truncated=any(value>limit for value in counts.values()))


@tool('assemble_architectural_model','Assemble multiple floor plans from final BLEND artifacts carrying original COM provenance. Verify original DWG hashes; preserve unresolved architectural parameters in the report.',
      {'project_path':{'type':'string','description':'JSON with evidence, final source BLEND paths and confirmed levels; no geometry exchange file'},
       'output':{'type':'string','description':'New .blend relative to ACAD_MCP_OUTPUT_ROOT'}},['project_path','output'])
def assemble_architectural_model(args):
    from .acad_core import output_path
    from .architectural_bridge import build_architecture
    with com_apartment():
        app=running_app()
        version=str(app.Version)
    outcome=build_architecture(args['project_path'],output_path(args['output'],{'.blend'}))
    return result(True,'Architectural draft built; unresolved items remain explicit.',autocad_version=version,**outcome)


@tool('assemble_ventilation_model', 'Add the Erdaut project ventilation overview from embedded read-only COM provenance to a preserved architectural BLEND. Project-specific rules and assumptions are required; no construction completeness is implied.',
      {'project_path': {'type': 'string'}, 'output': {'type': 'string', 'description': 'New .blend relative to ACAD_MCP_OUTPUT_ROOT'}},
      ['project_path', 'output'])
def assemble_ventilation_model(args):
    from .acad_core import output_path
    from .ventilation_bridge import build_ventilation
    outcome = build_ventilation(args['project_path'], output_path(args['output'], {'.blend'}))
    return result(True, 'Ventilation overview assembled; consult completeness and issue fields.', **outcome)
