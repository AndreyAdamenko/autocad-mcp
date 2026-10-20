"""Read-only drawing-set catalogue: sheet titles, text, dimensions and tables.

Produces an analysis report, never a geometry exchange file. Original DWGs are
read using the installed AutoCAD COM server, including table cell contents.
"""
from __future__ import annotations

import argparse
import json
import re
import traceback
from collections import Counter
from pathlib import Path

from .acad_core import com_apartment, running_app
from .com_read import method, maybe, open_source, read
from .extraction import drawing_spaces, source_hash
from .geometry import identity, insertion_matrix, multiply, transform


def inspect_document(doc, include_dimensions=True, scope='all'):
    if scope not in ('all', 'model', 'paper'):
        raise ValueError('Annotation scope must be all, model or paper')
    texts, dimensions, tables, issues = [], [], [], []
    types = Counter()

    def walk(space, space_name, matrix, ancestry=(), prefix=()):
        for i in range(int(read(space,"Count"))):
            obj = method(space,"Item",i)
            kind = str(read(obj,"ObjectName"))
            types[kind] += 1
            if 'Dimension' in kind and not include_dimensions:
                continue
            if kind not in ('AcDbText','AcDbMText','AcDbAttribute','AcDbAttributeDefinition','AcDbMLeader','AcDbTable','AcDbBlockReference') and 'Dimension' not in kind:
                continue
            handle = str(read(obj,"Handle"))
            source_id = "/".join((space_name,*prefix,handle))
            try:
                if kind in ("AcDbText","AcDbMText","AcDbAttribute","AcDbAttributeDefinition"):
                    text = str(read(obj,"TextString"))
                    point = maybe(obj,"InsertionPoint")
                    texts.append({"source_id":source_id,"text":text,"layer":str(read(obj,"Layer")),
                                  "point":transform(matrix,point) if point is not None else None})
                elif kind == "AcDbMLeader":
                    text = maybe(obj,"TextString")
                    if text:
                        texts.append({"source_id":source_id,"text":str(text),"layer":str(read(obj,"Layer")),
                                      "point":None,"note":"MLeader semantic text; placement not resolved"})
                elif "Dimension" in kind:
                    measurement = maybe(obj,"Measurement")
                    dimensions.append({"source_id":source_id,"type":kind,"measurement":measurement,
                                       "text_override":maybe(obj,"TextOverride"),
                                       "text_position":maybe(obj,"TextPosition"),
                                       "ExtLine1Point":maybe(obj,"ExtLine1Point"),"ExtLine2Point":maybe(obj,"ExtLine2Point"),
                                       "rotation":maybe(obj,"Rotation"),"layer":str(read(obj,"Layer")),
                                       "linear_scale_factor":maybe(obj,"LinearScaleFactor"),"parent_matrix":matrix})
                elif kind == "AcDbTable":
                    rows,cols=int(read(obj,"Rows")),int(read(obj,"Columns"))
                    if rows*cols>20000:
                        raise ValueError("Table too large for catalogue; use a dedicated bounded cell reader")
                    cells = [[str(method(obj,"GetText",r,c)) for c in range(cols)] for r in range(rows)]
                    tables.append({"source_id":source_id,"rows":cells,"point":maybe(obj,"InsertionPoint")})
                elif kind == "AcDbBlockReference":
                    name = str(read(obj,"Name"))
                    block = method(read(doc,"Blocks"),"Item",name)
                    if name in ancestry or len(ancestry)>=24 or bool(read(block,"IsXRef")):
                        continue
                    # Repeated decorative/table blocks need not be recatalogued
                    # per instance. Keep the first definition and every attribute.
                    if name not in seen_blocks:
                        seen_blocks.add(name)
                        local = insertion_matrix(list(read(obj,"InsertionPoint")),list(read(obj,"Normal")),float(read(obj,"Rotation")),
                                                 [float(read(obj,k+"ScaleFactor")) for k in "XYZ"],list(read(block,"Origin")))
                        walk(block,space_name,multiply(matrix,local),(*ancestry,name),(*prefix,handle))
                    if bool(maybe(obj,"HasAttributes",False)):
                        for accessor in ("GetAttributes","GetConstantAttributes"):
                            for attr in method(obj,accessor):
                                point=maybe(attr,"InsertionPoint")
                                texts.append({"source_id":source_id+"/"+str(read(attr,"Handle")),
                                              "text":str(read(attr,"TextString")),"tag":maybe(attr,"TagString"),
                                              "point":transform(matrix,point) if point is not None else None})
            except Exception as exc:
                issues.append({"source_id":source_id,"type":kind,"error":str(exc)})

    seen_blocks=set()
    spaces = drawing_spaces(doc, 'model' if scope == 'model' else 'all')
    for name,space in spaces:
        if scope == 'paper' and name == 'Model':
            continue
        walk(space,name,identity())
    title_pattern=r"план|разрез|фасад|сечени|высот|этаж|перекрыт|покрыти|кровл|ведомость.*чертеж"
    titles=[t for t in texts if re.search(title_pattern,t["text"],re.I)]
    return {"path":str(read(doc,"FullName")),"readonly":bool(read(doc,"ReadOnly")),
            "insunits":int(method(doc,"GetVariable","INSUNITS")),"types":dict(types),
            "titles":titles,"texts":texts,"dimensions":dimensions,"tables":tables,"issues":issues,
            "dimensions_included":include_dimensions, 'scope': scope,
            "note":"Repeated block definitions catalogued once; use full geometry traversal for instance placement"}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder",required=True,type=Path)
    p.add_argument("--names",nargs="*")
    p.add_argument("--report",required=True,type=Path)
    p.add_argument('--texts-only',action='store_true',help='First-pass catalogue without individual dimension properties')
    args=p.parse_args()
    paths=[args.folder/name for name in args.names] if args.names else sorted(args.folder.glob("*.dwg"))
    results=[]
    with com_apartment():
        app=running_app()
        for path in paths:
            before=None
            try:
                before=source_hash(path)
                doc=open_source(app,path)
                report=inspect_document(doc,include_dimensions=not args.texts_only)
                report.update(sha256_before=before,sha256_after=source_hash(path))
                report["source_unchanged"]=report["sha256_before"]==report["sha256_after"]
                if not report["source_unchanged"]:
                    raise RuntimeError("Source file changed during catalogue")
                results.append(report)
                print(json.dumps({"file":path.name,"texts":len(report["texts"]),"dimensions":len(report["dimensions"]),
                                  "tables":len(report["tables"]),"titles":[t["text"] for t in report["titles"][:16]]},ensure_ascii=False),flush=True)
            except Exception as exc:
                results.append({"path":str(path),"error":str(exc),"traceback":traceback.format_exc(),"sha256_before":before})
                print(json.dumps({"file":path.name,"error":str(exc)},ensure_ascii=False),flush=True)
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps({"drawings":results},ensure_ascii=False,indent=2,default=list),encoding="utf-8")


if __name__=="__main__":
    main()
