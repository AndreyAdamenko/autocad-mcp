"""Conservative hypotheses; explicit, sourced parameters gate solid modelling."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .geometry import norm, world_geometry

CATEGORIES = {
    "Walls": r"wall|стен|перегород",
    "Columns": r"column|колон|пилон",
    "Slabs": r"slab|floor|перекрыт|плит",
    "Doors": r"door|двер|(^|[ _-])др?\d",
    "Windows": r"window|окн|(^|[ _-])ок?\d",
    "Roofs": r"roof|кровл",
    "Stairs": r"stair|лестн",
}


def hypothesis(record):
    g = record["geometry"]
    text = " ".join(str(record.get(k, "")) for k in ("effective_layer", "layer"))
    text += " " + str(g.get("effective_name", ""))
    matches = [name for name, pattern in CATEGORIES.items() if re.search(pattern, text, re.I)]
    shape = []
    if g["kind"] == "polyline" and g.get("closed"):
        shape.append("closed contour")
        if len(g["vertices"]) == 4:
            shape.append("four vertices: possible column, opening or annotation frame")
    return {"source_id": record["id"], "candidates": matches, "confidence": 0.65 if len(matches) == 1 else 0.2,
            "confirmed": False, "evidence": {"layer_or_block": text.strip(), "shape": shape},
            "unknown_parameters": ["semantic role", "height", "vertical placement"]}


def elevation_candidate(record):
    g = record["geometry"]
    if g["kind"] != "text":
        return None
    match = re.search(r"(?<![\d\w])([+\-−±]\s*\d+[.,]\d{3})(?!\d)", g["text"])
    if not match:
        return None
    return {"source_id": record["id"], "text": g["text"], "numeric_text": match.group(1),
            "confirmed": False, "units": None,
            "reason": "Possible elevation label; datum, units and element association are unresolved"}


def validate_rules(rules):
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("category") not in CATEGORIES:
            raise ValueError("Each rule requires a known category")
        if not rule.get("source_id") and not rule.get("layer_pattern"):
            raise ValueError("A rule must select source_id or layer_pattern")
        if rule.get("layer_pattern"):
            re.compile(rule["layer_pattern"])
        if rule.get("confirmed") is not True or not rule.get("evidence"):
            raise ValueError("Solid rules require confirmed=true and evidence")
        for name in ("height_mm", "base_z_mm"):
            param = rule.get(name, {})
            value = param.get("value")
            if (param.get("confirmed") is not True or not param.get("source") or
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"{name} requires finite value, confirmed=true and source")
            if name == "height_mm" and value <= 0:
                raise ValueError("height_mm must be positive")
    return rules


def rule_for(record, rules):
    matches = [r for r in rules if (r.get("source_id") == record["id"] or
               (r.get("layer_pattern") and re.search(r["layer_pattern"], record.get("effective_layer", ""), re.I)))]
    if len(matches) > 1:
        raise ValueError(f"Conflicting solid rules for {record['id']}")
    return matches[0] if matches else None


def closed_line_contours(records, tolerance_mm=0.1):
    """Find unambiguous degree-two line cycles, grouped by space and layer.

    No gap filling, line extension or guessing at branched intersections.
    """
    groups = defaultdict(list)
    for record in records:
        if record["geometry"]["kind"] == "line" and record.get("status") == "ok":
            pts, _, _, _ = world_geometry(record)
            groups[(record["space"], record["effective_layer"])].append((record["id"], pts))
    result = []
    for (space, layer), lines in groups.items():
        adjacency = defaultdict(list)
        def key(p):
            return tuple(round(x/tolerance_mm) for x in p)
        for i, (_, pts) in enumerate(lines):
            adjacency[key(pts[0])].append(i)
            adjacency[key(pts[1])].append(i)
        visited = set()
        for first in range(len(lines)):
            if first in visited:
                continue
            stack, component = [first], []
            while stack:
                i = stack.pop()
                if i in visited:
                    continue
                visited.add(i)
                component.append(i)
                for p in lines[i][1]:
                    stack.extend(j for j in adjacency[key(p)] if j not in visited)
            nodes = {key(p) for i in component for p in lines[i][1]}
            if len(component) >= 3 and all(len(adjacency[k]) == 2 for k in nodes):
                result.append({"space": space, "layer": layer, "source_ids": [lines[i][0] for i in component],
                               "closed": True, "semantic_role": None, "tolerance_mm": tolerance_mm})
    return result


def analyse(records, rules=None):
    rules = validate_rules(rules or [])
    hypotheses, elevations = [], []
    counts = Counter()
    text_samples, dimensions = [], []
    for record in records:
        h = hypothesis(record)
        hypotheses.append(h)
        for name in h["candidates"]:
            counts[name] += 1
        elevation = elevation_candidate(record)
        if elevation:
            elevations.append(elevation)
        if record["geometry"]["kind"] == "text" and len(text_samples) < 300:
            text_samples.append({"id": record["id"], "text": record["geometry"]["text"]})
        if record["geometry"]["kind"] == "dimension":
            dimensions.append({"id": record["id"], **record["geometry"]})
    return {"hypothesis_counts": dict(counts), "hypotheses": hypotheses, "elevation_candidates": elevations,
            "closed_line_contours": closed_line_contours(records), "text_samples": text_samples,
            "dimensions": dimensions, "rules": rules, "assumed_heights": False,
            "warning": "Hypotheses and elevation text are not verified building parameters"}
