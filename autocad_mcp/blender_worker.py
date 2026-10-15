"""Blender bpy consumer. Protocol: header, records, successful end marker on stdin.

Run with blender --background --factory-startup --python-exit-code 3 --python ...
Only the final BLEND, QA renders, logs and verification reports are written.
"""
from __future__ import annotations

import json
import base64
import zlib
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocad_mcp.geometry import bounds, norm, ocs_basis, polyline_length, transform, world_geometry
from autocad_mcp.interpretation import analyse, rule_for, validate_rules


def collection(name, parent=None):
    result = bpy.data.collections.new(name[:63])
    (parent or bpy.context.scene.collection).children.link(result)
    return result


def mesh_object(name, points_mm, edges, faces, parent):
    # Keep single-precision mesh vertices close to the local origin. Object
    # translation is separately verified, so large WCS values cannot hide error.
    anchor = points_mm[0] if points_mm else [0, 0, 0]
    local = [[(p[i]-anchor[i])/1000 for i in range(3)] for p in points_mm]
    data = bpy.data.meshes.new(name)
    data.from_pydata(local, edges, faces)
    data.update()
    obj = bpy.data.objects.new(name, data)
    obj.location = [v/1000 for v in anchor]
    parent.objects.link(obj)
    return obj


def simple_footprint(points, tolerance):
    if len(points) < 3 or max(p[2] for p in points)-min(p[2] for p in points) > tolerance:
        raise ValueError("Extrusion requires a horizontal planar footprint")
    if len(points) > 10000:
        raise ValueError("Footprint too large for verified extrusion")
    def orient(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    n = len(points)
    for i in range(n):
        if norm([points[i][k]-points[(i+1) % n][k] for k in range(3)]) < 1e-7:
            raise ValueError("Footprint has duplicate/degenerate vertices")
        for j in range(i+2, n):
            if i == 0 and j == n-1:
                continue
            a,b,c,d = points[i], points[(i+1) % n], points[j], points[(j+1) % n]
            if orient(a,b,c)*orient(a,b,d) < 0 and orient(c,d,a)*orient(c,d,b) < 0:
                raise ValueError("Self-intersecting footprint")
    area = sum(points[i][0]*points[(i+1) % n][1]-points[(i+1) % n][0]*points[i][1] for i in range(n))/2
    if abs(area) < 1e-6:
        raise ValueError("Footprint has zero area")
    return points if area > 0 else list(reversed(points))


def extrude(points, rule, parent, source_id, tolerance):
    points = simple_footprint(points, tolerance)
    base, height = rule["base_z_mm"]["value"], rule["height_mm"]["value"]
    n = len(points)
    vertices = [[p[0], p[1], base] for p in points] + [[p[0], p[1], base+height] for p in points]
    # Triangulate concave caps using Blender's polygon tessellator.
    from mathutils.geometry import tessellate_polygon
    ring = [Vector((p[0]-points[0][0], p[1]-points[0][1], 0)) for p in points]
    index = {tuple(v): i for i, v in enumerate(ring)}
    triangles = tessellate_polygon([ring])
    # mathutils returns references to input vectors.
    cap = [[v if isinstance(v, int) else index[tuple(v)] for v in tri] for tri in triangles]
    faces = [list(reversed(f)) for f in cap] + [[i+n for i in f] for f in cap]
    faces += [[i, (i+1) % n, (i+1) % n+n, i+n] for i in range(n)]
    obj = mesh_object(rule["category"]+"_"+source_id, vertices, [], faces, parent)
    obj["source_id"], obj["rule_evidence"] = source_id, json.dumps(rule, ensure_ascii=True)
    obj["height_mm"], obj["base_z_mm"] = height, base
    return obj, vertices


def actual_points(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    data = evaluated.to_mesh()
    try:
        return [[float(x)*1000 for x in (evaluated.matrix_world @ v.co)] for v in data.vertices]
    finally:
        evaluated.to_mesh_clear()


def verify(objects, records, manifest, issues, tolerance):
    checks, failures, observed_by_id = [], [], {}
    bpy.context.view_layer.update()
    for obj, expected, record, role in objects:
        observed = actual_points(obj)
        if role == "reference":
            observed_by_id[record["id"]] = observed
        error = math.inf if len(expected) != len(observed) else max(
            (norm([a[i]-b[i] for i in range(3)]) for a,b in zip(expected, observed)), default=0)
        item = {"source_id": record["id"], "role": role, "object": obj.name,
                "vertices": len(observed), "max_coordinate_error_mm": error if math.isfinite(error) else None,
                "passed": error <= tolerance}
        # Independent AutoCAD metric when the entity has no enclosing block.
        g = record["geometry"]
        if role == "reference" and g["kind"] == "polyline" and g.get("length") is not None:
            scales = [norm([record["world_matrix"][i][j] for i in range(3)]) for j in range(3)]
            analytic_error = abs(polyline_length(g)-g["length"])*max(scales)
            item["polyline_analytic_length_error_mm"] = analytic_error
            item["passed"] &= analytic_error <= tolerance
        if role == "reference" and g["kind"] == "line":
            matrix = record["world_matrix"]
            scales = [norm([matrix[i][j] for i in range(3)]) for j in range(3)]
            if max(scales)-min(scales) < 1e-9:
                source_length = g["length"]*scales[0]
                measured_length = norm([observed[1][i]-observed[0][i] for i in range(3)])
                item.update(source_length_mm=source_length, measured_length_mm=measured_length)
                item["passed"] &= abs(source_length-measured_length) <= tolerance
        if role == "reference" and record.get("source_bbox") and record["id"].count("/") == 1:
            source_box = [[x*manifest["mm_per_unit"] for x in p] for p in record["source_bbox"]]
            measured_box = bounds(observed)
            # Nonzero width/thickness extends the COM envelope beyond a path.
            if g["kind"] in ("line", "arc", "circle", "ellipse", "face", "mesh"):
                box_error = max(abs(a-b) for p,q in zip(source_box, measured_box) for a,b in zip(p,q))
                item["source_bbox_error_mm"] = box_error
                item["passed"] &= box_error <= tolerance*2
        if role == "solid":
            measured = bounds(observed)
            item["height_error_mm"] = abs((measured[1][2]-measured[0][2])-obj["height_mm"])
            item["passed"] &= item["height_error_mm"] <= tolerance
            # Every undirected edge of a closed manifold must have two faces.
            edge_uses = Counter()
            for poly in obj.data.polygons:
                verts = list(poly.vertices)
                for i in range(len(verts)):
                    edge_uses[tuple(sorted((verts[i], verts[(i+1) % len(verts)])))] += 1
            item["closed_manifold"] = bool(edge_uses) and all(n == 2 for n in edge_uses.values())
            item["passed"] &= item["closed_manifold"]
        checks.append(item)
        if not item["passed"]:
            failures.append(item)
    # Check insert transforms against an independent COM bounding box, rather
    # than merely comparing two uses of our own matrix. Skip incomplete blocks.
    block_checks = []
    issue_ids = {i["source_id"] for i in issues}
    for record in records:
        if record["geometry"]["kind"] != "block" or not record.get("source_bbox"):
            continue
        prefix = record["id"]+"/"
        descendants = [r for r in records if r["id"].startswith(prefix) and r["geometry"]["kind"] != "block"]
        if not descendants or any(r["id"] not in observed_by_id or r["id"] in issue_ids for r in descendants):
            continue
        parent_inverse = Matrix(record["world_matrix"]).inverted()
        local_points = [[float(x) for x in (parent_inverse @ Vector(p))]
                        for r in descendants for p in observed_by_id[r["id"]]]
        actual_box = bounds(local_points)
        # Convert the comparison tolerance to parent-definition coordinates.
        scale_bound = max(norm([record["world_matrix"][i][j] for i in range(3)]) for j in range(3))
        source_box = record["source_bbox"]
        gap = max(abs(a-b) for p,q in zip(actual_box,source_box) for a,b in zip(p,q))*scale_bound
        violation = max(0, *(source_box[0][i]-actual_box[0][i] for i in range(3)),
                        *(actual_box[1][i]-source_box[1][i] for i in range(3)))*scale_bound
        item = {"source_id":record["id"], "role":"block_envelope", "source_bbox_error_mm":violation,
                "conservative_envelope_gap_mm":gap, "comparison":"containment in COM conservative envelope",
                "descendants":len(descendants), "passed":violation <= tolerance*3}
        block_checks.append(item)
        if not item["passed"]:
            failures.append(item)
    source_ids = {r["id"] for r in records}
    accounted = {r["id"] for r in records if r.get("status") == "ok" and r["geometry"]["kind"] == "block"}
    accounted.update(r["id"] for _,_,r,_ in objects)
    accounted.update(i["source_id"] for i in issues)
    missing = sorted(source_ids-accounted)
    return {"coordinate_checks_passed": not failures, "full_fidelity": not issues and not missing and not failures,
            "all_records_accounted_for": not missing, "missing_source_ids": missing,
            "tolerance_mm": tolerance, "checks": checks, "failures": failures,
            "block_envelope_checks": block_checks,
            "semantic_building_verified": False,
            "note": "Coordinate checks do not confirm architectural semantics or missing heights"}


def render_controls(target, points, qa_parent):
    scene = bpy.context.scene
    if not points:
        return []
    box = bounds(points)
    center = Vector([(box[0][i]+box[1][i])/2000 for i in range(3)])
    size = max((box[1][i]-box[0][i])/1000 for i in range(3)) or 1
    camera_data = bpy.data.cameras.new("QA camera")
    camera = bpy.data.objects.new("QA camera", camera_data)
    qa_parent.objects.link(camera)
    scene.camera = camera
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = size*1.3
    camera_data.clip_end = size*30+100
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"
    scene.display.shading.background_type = "WORLD"
    scene.world.color = (0.06, 0.06, 0.06)
    scene.render.resolution_x, scene.render.resolution_y = 1600, 1600
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    paths = []
    for label, offset in (("plan", Vector((0, 0, size*2+1))), ("iso", Vector((1, -1.3, 1.2)).normalized()*(size*2+1))):
        camera.location = center+offset
        camera.rotation_euler = (-offset).to_track_quat("-Z", "Y").to_euler()
        path = target.with_name(target.stem+f".{label}.png")
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        paths.append(str(path))
    return paths


def main():
    header = json.loads(sys.stdin.buffer.readline())
    if header.get("kind") != "header":
        raise ValueError("Missing pipeline header")
    target, manifest = Path(header["target"]), header["manifest"]
    if manifest.get("mm_per_unit") is None:
        raise ValueError("Unresolved source units")
    rules, tolerance = validate_rules(header["rules"]), header["tolerance_mm"]
    records, ending = [], None
    for line in sys.stdin.buffer:
        item = json.loads(line)
        if item["kind"] == "end":
            ending = item["scan"]
            break
        if item["kind"] != "record":
            raise ValueError("Invalid pipeline message")
        records.append(item["record"])
    if not ending or not ending["done"] or not ending["source_unchanged"]:
        raise ValueError("Source scan incomplete or source DWG changed")
    if len({r["id"] for r in records}) != len(records) or len(records) != ending["counts"]["records"]:
        raise ValueError("Source record count/identity mismatch")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.unit_settings.system, scene.unit_settings.scale_length = "METRIC", 1
    scene["source_sha256"] = manifest["source_sha256"]
    scene["source_path"] = manifest["source_path"]
    scene["tolerance_mm"] = tolerance
    source_parent = collection("CAD reference")
    model_parent = collection("Reconstructed elements")
    qa_parent = collection("QA visual aids")
    layers, models, visual_lines = {}, {}, defaultdict(list)
    objects, issues, render_points = [], [], []
    counts = Counter(source_records=len(records))
    for record in records:
        source_id, g = record["id"], record["geometry"]
        name = record.get("space", "unknown")+" / "+record.get("effective_layer", "unknown")
        if name not in layers:
            layers[name] = collection(name, source_parent)
        parent = layers[name]
        if record.get("status") != "ok":
            issues.append({"source_id": source_id, "type": record.get("type"), "reason": record.get("error") or g.get("reason", "Unsupported source geometry")})
            counts["unsupported"] += 1
            continue
        if g["kind"] == "block":
            counts["block_containers"] += 1
            continue
        if g["kind"] in ("text", "dimension"):
            data = bpy.data.curves.new("Annotation "+source_id, "FONT")
            data.body = g.get("text", g.get("text_override") or str(g.get("measurement", "")))
            data.size = max(float(g.get("height", g.get("TextHeight", 100)))*manifest["mm_per_unit"]/1000, 0.0001)
            obj = bpy.data.objects.new(source_id, data)
            point = transform(record["world_matrix"], g["point"])
            obj.location = [v/1000 for v in point]
            parent.objects.link(obj)
            obj["source_id"], obj["source_annotation"] = source_id, json.dumps(g, ensure_ascii=True)
            obj.color = (0.6, 0.8, 0.9, 1)
            counts["annotations"] += 1
            issues.append({"source_id": source_id, "reason": "Annotation content retained; AutoCAD font, alignment and dimension graphics are not reproduced exactly"})
            continue
        try:
            pts, edges, faces, closed = world_geometry(record, tolerance)
            obj = mesh_object(source_id, pts, edges, faces, parent)
            obj["source_id"], obj["source_type"] = source_id, record["type"]
            obj.color = (0.65, 0.75, 0.85, 1)
            objects.append((obj, pts, record, "reference"))
            counts["reference_geometry"] += 1
            if g["kind"] == "spline":
                issues.append({"source_id": source_id, "reason": "Exact NURBS retained; adaptive preview has no certified global approximation bound"})
            if not record.get("visible", True):
                obj.hide_render = True
            else:
                render_points.extend(pts)
                if edges:
                    visual_lines[name].append((pts, edges))
            if g.get("thickness") or any(w and any(abs(x) > 1e-10 for x in w) for w in g.get("widths", [])):
                issues.append({"source_id": source_id, "reason": "Path is transferred; AutoCAD width/thickness is retained in provenance, not reconstructed"})
            rule = rule_for(record, rules)
            if rule:
                if not closed and not faces:
                    raise ValueError("Solid rule requires a closed footprint")
                category = rule["category"]
                if category not in models:
                    models[category] = collection(category, model_parent)
                solid, expected = extrude(pts, rule, models[category], source_id, tolerance)
                solid.color = (0.8, 0.65, 0.45, 1)
                objects.append((solid, expected, record, "solid"))
                render_points.extend(expected)
                counts["solids"] += 1
        except Exception as exc:
            issues.append({"source_id": source_id, "reason": str(exc)})
            counts["build_errors"] += 1
    # Render-only curves make the exact loose-edge reference meshes visible.
    render_box = bounds(render_points)
    visual_radius = max(0.003, max((render_box[1][i]-render_box[0][i])/1000 for i in range(3))/1600*0.65) if render_box else 0.003
    for name, paths in visual_lines.items():
        curve = bpy.data.curves.new("QA "+name, "CURVE")
        curve.dimensions, curve.bevel_depth, curve.bevel_resolution = "3D", visual_radius, 0
        for pts, edges in paths:
            for a,b in edges:
                spline = curve.splines.new("POLY")
                spline.points.add(1)
                for point,p in zip(spline.points, (pts[a], pts[b])):
                    point.co = (*[x/1000 for x in p], 1)
        aid = bpy.data.objects.new("QA "+name, curve)
        aid.color = (0.9, 0.95, 1, 1)
        aid["role"] = "render aid; bevel is not source geometry"
        qa_parent.objects.link(aid)
    report = {"manifest": manifest, "counts": dict(counts), "source_unchanged": ending["source_unchanged"],
              "source_sha256_after": ending["source_sha256_after"], "issues": issues,
              "analysis": analyse(records, rules), "blender_version": bpy.app.version_string,
              "verification": verify(objects, records, manifest, issues, tolerance)}
    report["renders"] = render_controls(target, render_points, qa_parent)
    target.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    # Store the raw COM provenance inside the final BLEND, not in an exchange file.
    print("Embedding compressed COM provenance and verification report", flush=True)
    source_data = json.dumps({"manifest": manifest}, ensure_ascii=True)+"\n"
    source_data += "".join(json.dumps(record, ensure_ascii=True)+"\n" for record in records)
    provenance = bpy.data.texts.new("AutoCAD source provenance.jsonl.zlib.base85")
    provenance.write(base64.b85encode(zlib.compress(source_data.encode("utf-8"))).decode("ascii"))
    qa_report = bpy.data.texts.new("AutoCAD verification report.json.zlib.base85")
    qa_report.write(base64.b85encode(zlib.compress(json.dumps(report, ensure_ascii=True, allow_nan=False).encode())).decode("ascii"))
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    print(json.dumps({"blend": str(target), "counts": dict(counts),
                      "coordinates_passed": report["verification"]["coordinate_checks_passed"],
                      "full_fidelity": report["verification"]["full_fidelity"]}))


if __name__ == "__main__":
    main()
