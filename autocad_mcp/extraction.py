"""Original DWG geometry read exclusively through AutoCAD ActiveX.

No Explode, Copy, Export, Save, SendCommand, temporary entities or selection sets.
Unsupported geometry remains an explicit record, never an invented bounding box.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections import Counter
from pathlib import Path

from .acad_core import MM_PER_DRAWING_UNIT, com_apartment, running_app
from .com_read import BUSY_HRESULTS, method, maybe, open_source, read, retry, typed_entity
from .geometry import identity, insertion_matrix, multiply, ocs_basis, transform


def triples(values):
    values = [float(v) for v in values]
    if len(values) % 3:
        raise ValueError("invalid 3D coordinate array")
    return [values[i:i+3] for i in range(0, len(values), 3)]


def bbox(obj):
    try:
        # [out] parameters: keep type-library marshalling, do not FlagAsMethod.
        lo, hi = retry(lambda: obj.GetBoundingBox())
        return [[float(x) for x in lo], [float(x) for x in hi]]
    except Exception:
        return None


def entity_geometry(obj, type_name):
    p = lambda key: [float(x) for x in read(obj, key)]
    f = lambda key: float(read(obj, key))
    if type_name == 'AcDbMLeader':
        # Read the callout anchors without creating/exploding leader graphics.
        leaders, errors = [], []
        count = int(maybe(obj, 'LeaderCount', 0))
        seen = set()
        # Deleted leaders leave sparse indexes; LeaderCount is not a maximum ID.
        for candidate in range(64 + count * 8 if count else 0):
            try:
                index = int(method(obj, 'GetLeaderIndex', candidate))
            except Exception as exc:
                if getattr(exc, 'hresult', None) in BUSY_HRESULTS:
                    raise
                continue
            if index in seen:
                continue
            seen.add(index)
            try:
                indexes = method(obj, 'GetLeaderLineIndexes', index)
                for line_index in indexes:
                    leaders.append({'index': int(line_index),
                                    'vertices': triples(method(obj, 'GetLeaderLineVertices', int(line_index)))})
            except Exception as exc:
                errors.append(str(exc))
            if len(seen) >= count:
                break
        if len(seen) < count:
            errors.append('Leader indexes could not all be found within the bounded search')
        return {'kind': 'unsupported', 'reason': 'MLeader graphics not reconstructed; semantic text and readable anchors retained',
                'text': maybe(obj, 'TextString'), 'leader_lines': leaders, 'leader_errors': errors}
    if type_name == 'AcDbMline':
        return {'kind': 'unsupported', 'reason': 'MLine centre path retained; style offset graphics not reconstructed',
                'centerline': triples(read(obj, 'Coordinates')), 'style': maybe(obj, 'StyleName'),
                'scale': maybe(obj, 'MLineScale'), 'justification': maybe(obj, 'Justification')}
    if type_name == "AcDbLine":
        return {"kind": "line", "start": p("StartPoint"), "end": p("EndPoint"), "length": f("Length"),
                'thickness': maybe(obj, 'Thickness', 0), 'normal': maybe(obj, 'Normal', [0, 0, 1])}
    if type_name in ("AcDbPolyline", "AcDb2dPolyline", "AcDb3dPolyline"):
        raw = [float(x) for x in read(obj, "Coordinates")]
        closed = bool(read(obj, "Closed"))
        if type_name == "AcDb3dPolyline":
            return {"kind": "polyline", "vertices": triples(raw), "closed": closed, "bulges": [], "normal": [0, 0, 1]}
        normal, elevation = p("Normal"), f("Elevation")
        if type_name == "AcDb2dPolyline" and int(read(obj, "Type")) != 0:
            return {"kind": "unsupported", "reason": "Curve-fit/spline polyline: Coordinates are control vertices", "coordinates": raw}
        width = 2 if type_name == "AcDbPolyline" else 3
        ocs = [[raw[i], raw[i+1], elevation] for i in range(0, len(raw), width)]
        b = ocs_basis(normal)
        vertices = [transform(b, v) for v in ocs]
        bulges = [float(method(obj, "GetBulge", i)) for i in range(len(vertices))]
        widths = []
        for i in range(len(vertices)):
            try:
                widths.append(list(retry(lambda: obj.GetWidth(i))))  # [out]
            except Exception:
                widths.append(None)
        return {"kind": "polyline", "vertices": vertices, "closed": closed,
                "ocs_vertices": ocs, "normal": normal, "elevation": elevation,
                "bulges": bulges, "widths": widths, "length": maybe(obj, "Length"),
                "thickness": maybe(obj, "Thickness", 0)}
    if type_name in ("AcDbCircle", "AcDbArc"):
        g = {"kind": "circle" if type_name == "AcDbCircle" else "arc",
             "center": p("Center"), "radius": f("Radius"), "normal": p("Normal"),
             "thickness": maybe(obj, "Thickness", 0)}
        if type_name == "AcDbArc":
            start, end = f("StartAngle"), f("EndAngle")
            g.update(start_angle=start, end_angle=end, sweep=(end-start) % (2*math.pi),
                     start=p("StartPoint"), end=p("EndPoint"), length=f("ArcLength"))
        return g
    if type_name == "AcDbEllipse":
        start, end = f("StartParameter"), f("EndParameter")
        return {"kind": "ellipse", "center": p("Center"), "normal": p("Normal"),
                "major_axis": p("MajorAxis"), "ratio": f("RadiusRatio"),
                "start_parameter": start, "sweep_parameter": (end-start) or 2*math.pi}
    if type_name == "AcDbPoint":
        return {"kind": "point", "point": p("Coordinates")}
    if type_name in ("AcDbFace", "AcDb3dFace", "AcDbSolid", "AcDbTrace"):
        vertices = triples(read(obj, "Coordinates"))
        order = list(range(len(vertices)))
        if type_name in ("AcDbSolid", "AcDbTrace") and len(order) == 4:
            order = [0, 1, 3, 2]
        if len(vertices) == 4 and vertices[2] == vertices[3]:
            order = [0, 1, 2]
        return {"kind": "face", "vertices": vertices, "faces": [order]}
    if type_name == "AcDbPolygonMesh":
        if int(read(obj, "Type")) != 0:
            return {"kind": "unsupported", "reason": "Fitted polygon mesh lacks evaluated topology"}
        vertices = triples(read(obj, "Coordinates"))
        m, n = int(read(obj, "MVertexCount")), int(read(obj, "NVertexCount"))
        faces = []
        for i in range(m if bool(read(obj, "MClose")) else m-1):
            for j in range(n if bool(read(obj, "NClose")) else n-1):
                faces.append([i*n+j, ((i+1) % m)*n+j, ((i+1) % m)*n+(j+1) % n, i*n+(j+1) % n])
        return {"kind": "mesh", "vertices": vertices, "faces": faces}
    if type_name == "AcDbSpline":
        control_points = triples(read(obj, "ControlPoints"))
        rational = bool(read(obj, "IsRational"))
        return {"kind": "spline", "control_points": control_points,
                "knots": list(read(obj, "Knots")), "weights": list(read(obj, "Weights")) if rational else [1.0]*len(control_points),
                "rational": rational,
                "degree": int(read(obj, "Degree")), "closed": bool(read(obj, "Closed")),
                "periodic": bool(read(obj, "IsPeriodic"))}
    if type_name in ("AcDbText", "AcDbMText", "AcDbAttribute", "AcDbAttributeDefinition"):
        return {"kind": "text", "text": str(read(obj, "TextString")), "point": p("InsertionPoint"),
                "height": f("Height"), "rotation": f("Rotation"), "normal": p("Normal"),
                "alignment_point": maybe(obj, "TextAlignmentPoint"), "alignment": maybe(obj, "Alignment"),
                "width_factor": maybe(obj, "ScaleFactor", 1), "tag": maybe(obj, "TagString"),
                "style": maybe(obj, "StyleName"), "attachment": maybe(obj, "AttachmentPoint")}
    if "Dimension" in type_name:
        angular = "Angular" in type_name
        g = {"kind": "dimension", "measurement": f("Measurement"),
             "measurement_unit": "radians" if angular else "drawing_units",
             "text_override": str(maybe(obj, "TextOverride", "")), "point": p("TextPosition"),
             "style": maybe(obj, "StyleName"), "linear_scale_factor": maybe(obj, "LinearScaleFactor")}
        for key in ("ExtLine1Point", "ExtLine2Point", "DimLinePoint", "Center", "ChordPoint", "LeaderLength", "Rotation", "TextHeight"):
            value = maybe(obj, key)
            if value is not None:
                g[key] = list(value) if isinstance(value, tuple) else value
        return g
    if type_name == "AcDb3dSolid":
        return {"kind": "unsupported", "reason": "ActiveX 2014 exposes solid metrics, no read-only face tessellation",
                "volume": f("Volume"), "centroid": p("Centroid"), "solid_type": maybe(obj, "SolidType")}
    if type_name == "AcDbPolyFaceMesh":
        return {"kind": "unsupported", "reason": "PolyfaceMesh.Faces is write-only in acax19; topology cannot be read",
                "vertices": triples(read(obj, "Coordinates")), "number_of_faces": int(read(obj, "NumberOfFaces"))}
    return {"kind": "unsupported", "reason": f"No verified read-only geometry adapter for {type_name}"}


def drawing_spaces(doc, scope):
    if scope == "model":
        return [("Model", read(doc, "ModelSpace"))]
    if scope != "all":
        raise ValueError("scope must be model or all")
    layouts = read(doc, "Layouts")
    result = []
    for i in range(int(read(layouts, "Count"))):
        layout = method(layouts, "Item", i)
        result.append((str(read(layout, "Name")), retry(lambda: read(layout, "Block"), required_metadata=True)))
    return result


def iter_records(doc, spaces, mm_per_unit, max_depth=32, visible_only=False):
    scale = identity()
    for i in range(3):
        scale[i][i] = mm_per_unit if mm_per_unit is not None else 1

    metadata_cache, geometry_cache, block_cache = {}, {}, {}
    geometry_cache_bytes = 0

    def visit(obj, matrix, path, space, inherited_layer="0", ancestry=()):
        nonlocal geometry_cache_bytes
        handle = str(read(obj, 'Handle'))
        if handle not in metadata_cache:
            if len(metadata_cache) >= 10000:
                metadata_cache.pop(next(iter(metadata_cache)))
            type_name = str(read(obj, 'ObjectName'))
            visible = bool(maybe(obj, 'Visible', True))
            metadata_cache[handle] = (type_name, str(read(obj, 'Layer')), visible,
                                      None if visible_only and not visible else bbox(obj))
        type_name, layer, visible, source_bbox = metadata_cache[handle]
        layer_effective = inherited_layer if layer == "0" else layer
        row = {"id": "/".join([space, *path, handle]), "handle": handle, "type": type_name,
               "layer": layer, "effective_layer": layer_effective, "space": space,
               "world_matrix": matrix, "visible": visible,
               "source_bbox": source_bbox, "status": "ok"}
        if visible_only and not visible:
            row.update(status='excluded', geometry={'kind': 'excluded', 'reason': 'Explicitly excluded invisible block variant geometry'})
            yield row
            return
        try:
            if type_name in ("AcDbBlockReference", "AcDbMInsertBlock"):
                obj = typed_entity(obj, type_name)
                name = str(read(obj, "Name"))
                if name not in block_cache:
                    if len(block_cache) >= 128:
                        block_cache.pop(next(iter(block_cache)))
                    block = method(read(doc, "Blocks"), "Item", name)
                    block_cache[name] = (block, None)
                block, children = block_cache[name]
                normal = list(read(obj, "Normal"))
                scales = [float(read(obj, k+"ScaleFactor")) for k in "XYZ"]
                insertion, origin = list(read(obj, "InsertionPoint")), list(read(block, "Origin"))
                rotation = float(read(obj, "Rotation"))
                g = {"kind": "block", "name": name, "effective_name": str(maybe(obj, "EffectiveName", name)),
                     "insertion": insertion, "origin": origin, "normal": normal, "scales": scales,
                     "effective_scales": [maybe(obj, k+"EffectiveScaleFactor") for k in "XYZ"],
                     "rotation": rotation, "definition_units": maybe(block, "Units"),
                     "insunits_factor": maybe(obj, "InsUnitsFactor"), "xref": bool(read(block, "IsXRef")),
                     "child_count": int(read(block, "Count"))}
                if bool(maybe(obj, 'IsDynamicBlock', False)):
                    try:
                        g['dynamic_properties'] = [{'name': str(read(prop, 'PropertyName')),
                                                     'value': read(prop, 'Value'),
                                                     'units_type': maybe(prop, 'UnitsType')}
                                                    for prop in method(obj, 'GetDynamicBlockProperties')]
                    except Exception as exc:
                        g['dynamic_property_error'] = str(exc)
                row["geometry"] = g
                if name in ancestry or len(ancestry) >= max_depth:
                    row.update(status="unsupported", error="Block cycle or maximum nesting depth")
                    yield row
                    return
                if type_name == "AcDbMInsertBlock":
                    row.update(status="unsupported", error="MInsert replication not yet verified")
                    yield row
                    return
                if g["xref"]:
                    row.update(status="unsupported", error="External reference visibility/clipping not verified")
                    yield row
                    return
                yield row
                child_matrix = multiply(matrix, insertion_matrix(insertion, normal, rotation, scales, origin))
                if children is None and g['child_count'] <= 1024:
                    children = [method(block, 'Item', i) for i in range(g['child_count'])]
                    block_cache[name] = (block, children)
                for j in range(g["child_count"]):
                    try:
                        child = children[j] if children is not None else method(block,'Item',j)
                        yield from visit(child, child_matrix, [*path, handle], space, layer_effective, (*ancestry, name))
                    except Exception as exc:
                        yield {"id": row["id"]+f"/error-{j}", "status": "error", "error": str(exc), "space": space,
                               "type": "unreadable_block_child", "geometry": {"kind": "unsupported"}}
                if bool(maybe(obj, "HasAttributes", False)):
                    # Attribute coordinates already belong to the enclosing space.
                    for accessor in ("GetAttributes", "GetConstantAttributes"):
                        for attr in method(obj, accessor):
                            yield from visit(attr, matrix, [*path, handle, accessor], space, layer_effective, ancestry)
                return
            if handle in geometry_cache:
                geometry = geometry_cache[handle][0]
            else:
                geometry = entity_geometry(typed_entity(obj, type_name), type_name)
                size=len(json.dumps(geometry,ensure_ascii=True))
                # Cache small repeated definition entities only. A huge mesh is
                # streamed normally, without another long-lived cached copy.
                if size <= 65536:
                    while geometry_cache and (len(geometry_cache)>=4096 or geometry_cache_bytes+size>16*1024*1024):
                        _,weight=geometry_cache.pop(next(iter(geometry_cache)))
                        geometry_cache_bytes-=weight
                    geometry_cache[handle]=(geometry,size)
                    geometry_cache_bytes+=size
            row["geometry"] = geometry
            if row["geometry"]["kind"] == "unsupported":
                row["status"] = "unsupported"
        except Exception as exc:
            row.update(status="error", error=str(exc), geometry={"kind": "unsupported", "reason": str(exc)})
        yield row

    for name, space in spaces:
        for i in range(int(read(space, "Count"))):
            try:
                yield from visit(method(space, "Item", i), scale, [], name)
            except Exception as exc:
                yield {"id": f"{name}/error-{i}", "space": name, "type": "unreadable_entity",
                       "status": "error", "geometry": {"kind": "unsupported", "reason": str(exc)}}


def source_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024*1024), b""):
            h.update(part)
    return h.hexdigest()


class GeometryScan:
    def __init__(self, doc, scope="all", mm_per_unit=None, units_evidence=None):
        if not bool(read(doc, "ReadOnly")):
            raise RuntimeError("Geometry scans require a read-only source document")
        self.doc, self.spaces = doc, drawing_spaces(doc, scope)
        self.path = str(read(doc, "FullName"))
        code = int(method(doc, "GetVariable", "INSUNITS"))
        if mm_per_unit is not None:
            if isinstance(mm_per_unit, bool) or not math.isfinite(mm_per_unit) or mm_per_unit <= 0 or not units_evidence:
                raise ValueError("Unit override requires positive mm_per_unit and units_evidence")
        else:
            mm_per_unit = MM_PER_DRAWING_UNIT.get(code)
        self.baseline = self.fingerprint()
        self.before_hash = source_hash(self.path)
        layers = read(doc, "Layers")
        layer_info = []
        for i in range(int(read(layers, "Count"))):
            layer = method(layers, "Item", i)
            layer_info.append({key: maybe(layer, key) for key in ("Name", "LayerOn", "Freeze", "Lock", "Color", "Linetype")})
        self.manifest = {"schema": "autocad-com-geometry/1", "source_path": self.path,
                         "source_sha256": self.before_hash, "autocad_version": str(read(read(doc, "Application"), "Version")),
                         "readonly": True, "insunits": code, "mm_per_unit": mm_per_unit,
                         "units_evidence": units_evidence or ("INSUNITS" if mm_per_unit else None),
                         "coordinate_system": "WCS", "angles": "radians", "geometry_units": "drawing_units",
                         "world_matrix_target_units": "mm" if mm_per_unit else "unresolved_drawing_units",
                         "spaces": [{"name": name, "root_entities": int(read(space, "Count"))} for name, space in self.spaces],
                         "layers": layer_info,
                         "ucs": {key: list(method(doc, "GetVariable", key)) for key in ("UCSORG", "UCSXDIR", "UCSYDIR")}}
        self.iterator = iter_records(doc, self.spaces, mm_per_unit)
        self.counts, self.page, self.finished = Counter(), 0, False
        self.pending = None
        self.created = time.monotonic()

    def select_root_handles(self, handles, evidence, visible_only=False):
        """Restrict a fresh scan to proved ModelSpace roots, preserving provenance.

        Selecting a child directly would lose its block transform and is rejected.
        Fingerprints continue to cover the original spaces, not a synthetic list.
        """
        if self.page != 0 or self.pending is not None:
            raise ValueError('Root selection requires a fresh scan')
        if type(visible_only) is not bool:
            raise ValueError('visible_only must be a boolean')
        if not isinstance(handles, list) or not 1 <= len(handles) <= 10000 or not evidence:
            raise ValueError('A nonempty bounded handle list and selection evidence are required')
        if any(not isinstance(h, str) or not h or any(c not in '0123456789abcdefABCDEF' for c in h) for h in handles):
            raise ValueError('Root selections use plain hexadecimal handles')
        if len({h.upper() for h in handles}) != len(handles):
            raise ValueError('Duplicate root handles are not allowed')
        model = read(self.doc, 'ModelSpace')
        owner = int(read(model, 'ObjectID'))
        objects = []
        for handle in handles:
            obj = method(self.doc, 'HandleToObject', handle)
            if int(read(obj, 'OwnerID')) != owner:
                raise ValueError('Selected handle is not a ModelSpace root: ' + handle)
            objects.append(obj)

        class SelectedSpace:
            Count = len(objects)

            def Item(self, index):
                return objects[index]

        self.manifest['selection'] = {'root_handles': handles, 'evidence': str(evidence),
                                      'selected_roots': len(objects), 'whole_drawing': False,
                                      'visible_only': visible_only}
        self.iterator = iter_records(self.doc, [('Model', SelectedSpace())], self.manifest['mm_per_unit'], visible_only=visible_only)

    def fingerprint(self):
        return (str(read(self.doc, "FullName")), bool(read(self.doc, "ReadOnly")),
                int(method(self.doc, "GetVariable", "DBMOD")),
                tuple((name, int(read(space, "Count"))) for name, space in self.spaces))

    def batch(self, page, limit=100, max_bytes=256000):
        if type(page) is not int or page != self.page:
            raise ValueError(f"Expected page {self.page}; cursors are sequential and must not be replayed")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        if type(max_bytes) is not int or not 4096 <= max_bytes <= 1000000:
            raise ValueError("max_bytes must be 4096..1000000")
        if self.fingerprint() != self.baseline:
            raise RuntimeError("Source document changed during scan; discard all batches and restart")
        items = []
        used = 1024
        while len(items) < limit and used < max_bytes-512:
            if self.pending is None:
                try:
                    record = next(self.iterator)
                except StopIteration:
                    self.finished = True
                    break
                self.counts["records"] += 1
                self.counts[record["status"]] += 1
                encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
                self.pending = (record["id"], encoded, 0)
            record_id, encoded, offset = self.pending
            # Escaping a JSON fragment can double its size. ASCII avoids UTF-8 splits.
            size = min(len(encoded)-offset, max(1, (max_bytes-used-512)//2))
            item = {"record_id": record_id, "offset": offset, "total_chars": len(encoded),
                    "json_fragment": encoded[offset:offset+size]}
            actual_size = len(json.dumps(item, ensure_ascii=True).encode("utf-8"))
            if actual_size + used > max_bytes:
                break
            items.append(item)
            used += actual_size
            offset += size
            self.pending = None if offset == len(encoded) else (record_id, encoded, offset)
        if self.fingerprint() != self.baseline:
            raise RuntimeError("Source document changed while reading this batch; discard scan")
        result = {"page": self.page, "next_page": None if self.finished else self.page+1,
                  "done": self.finished, "fragments": items, "counts": dict(self.counts)}
        if self.finished:
            after = source_hash(self.path)
            result.update(source_sha256_after=after, source_unchanged=after == self.before_hash)
            if after != self.before_hash:
                raise RuntimeError("Source DWG hash changed; scan invalid")
        self.page += 1
        return result


class RecordAssembler:
    def __init__(self):
        self.pending = {}

    def accept(self, fragments):
        records = []
        for f in fragments:
            parts, offset = self.pending.get(f["record_id"], ([], 0))
            if offset != f["offset"]:
                raise ValueError("Missing/replayed geometry fragment")
            parts.append(f["json_fragment"])
            offset += len(f["json_fragment"])
            if offset == f["total_chars"]:
                record = json.loads("".join(parts))
                if record["id"] != f["record_id"]:
                    raise ValueError("Fragment identity mismatch")
                records.append(record)
                self.pending.pop(f["record_id"], None)
            elif offset < f["total_chars"]:
                self.pending[f["record_id"]] = (parts, offset)
            else:
                raise ValueError("Geometry fragment exceeds declared record size")
        return records
