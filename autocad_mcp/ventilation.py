"""Numerical helpers for drawing-backed ventilation envelopes, in millimetres.

Sections and elevations must be supplied with evidence by the caller. Sweeps
describe the outer occupied shape, not sheet-metal quantities or airflow.
"""
import math
import re

from .geometry import cross, norm, normalise


def plain_text(value):
    value = re.sub(r'%%[cC]', 'Ø', value).replace('\\P', ' ').replace('\\~', ' ')
    value = re.sub(r'\\[A-Za-z][^;]*;', '', value)
    return value.replace('{', '').replace('}', '')


def section_from_text(value):
    value = plain_text(value)
    rectangular = re.search(r'(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)', value, re.I)
    if rectangular:
        width, height = (float(s.replace(',', '.')) for s in rectangular.groups())
        return {'shape': 'rectangular', 'width_mm': width, 'height_mm': height}
    round_section = re.search(r'(?:Ø|DN|Ду)\s*(\d+(?:[.,]\d+)?)', value, re.I)
    if round_section:
        return {'shape': 'round', 'diameter_mm': float(round_section.group(1).replace(',', '.'))}
    return None


def segment_distance(point, start, end):
    delta = [end[i] - start[i] for i in range(2)]
    length2 = sum(v * v for v in delta)
    if length2 <= 1e-12:
        return math.dist(point[:2], start[:2]), 0.0
    t = max(0.0, min(1.0, sum((point[i] - start[i]) * delta[i] for i in range(2)) / length2))
    return math.dist(point[:2], [start[i] + t * delta[i] for i in range(2)]), t


def path_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def sweep(points, section, end_section=None, sides=32):
    """Closed envelope along a spatial path, with optional taper and end caps."""
    if len(points) < 2 or any(len(p) != 3 or not all(math.isfinite(x) for x in p) for p in points):
        raise ValueError('A finite spatial path with at least two points is required')
    directions = []
    for a, b in zip(points, points[1:]):
        delta = [b[i] - a[i] for i in range(3)]
        if norm(delta) <= 1e-8:
            raise ValueError('Consecutive path vertices must differ')
        directions.append(normalise(delta))
    end_section = end_section or section
    if section['shape'] != end_section['shape'] or section['shape'] not in ('round', 'rectangular'):
        raise ValueError('Taper sections must have the same supported shape')
    keys = ('diameter_mm',) if section['shape'] == 'round' else ('width_mm', 'height_mm')
    if any(not math.isfinite(s[k]) or s[k] <= 0 for s in (section, end_section) for k in keys):
        raise ValueError('Section dimensions must be finite and positive')
    if sides < 8 or sides % 4:
        raise ValueError('Circle sides must be a multiple of four, at least eight')
    count = sides if section['shape'] == 'round' else 4
    distances = [0.0]
    for a, b in zip(points, points[1:]):
        distances.append(distances[-1] + math.dist(a, b))
    vertices, faces, previous_u = [], [], None
    for index, point in enumerate(points):
        if index == 0:
            tangent = directions[0]
        elif index == len(points) - 1:
            tangent = directions[-1]
        else:
            candidate = [directions[index - 1][i] + directions[index][i] for i in range(3)]
            if norm(candidate) < 1e-8:
                raise ValueError('A reversing path needs explicit fitting topology')
            tangent = normalise(candidate)
        if previous_u is None:
            reference = [0, 0, 1] if abs(tangent[2]) < .9 else [0, 1, 0]
            u = normalise(cross(reference, tangent))
        else:
            dot = sum(previous_u[i] * tangent[i] for i in range(3))
            projected = [previous_u[i] - dot * tangent[i] for i in range(3)]
            if norm(projected) < 1e-8:
                raise ValueError('Frame singularity needs an explicit path split')
            u = normalise(projected)
        v = cross(tangent, u)
        previous_u = u
        fraction = distances[index] / distances[-1]
        values = {k: section[k] + fraction * (end_section[k] - section[k]) for k in keys}
        if section['shape'] == 'round':
            radius = values['diameter_mm'] / 2
            ring = [(radius * math.cos(2 * math.pi * j / count), radius * math.sin(2 * math.pi * j / count)) for j in range(count)]
        else:
            w, h = values['width_mm'] / 2, values['height_mm'] / 2
            ring = [(-w, -h), (w, -h), (w, h), (-w, h)]
        vertices.extend([[point[i] + a * u[i] + b * v[i] for i in range(3)] for a, b in ring])
    for ring in range(len(points) - 1):
        for j in range(count):
            k = (j + 1) % count
            faces.append([ring * count + j, ring * count + k, (ring + 1) * count + k, (ring + 1) * count + j])
    faces.extend([list(reversed(range(count))), list(range((len(points) - 1) * count, len(points) * count))])
    return vertices, faces


def rectangular_body(lines, minimum_length=100, expected_width=None):
    """Find the two long visible edges, excluding short flange cross-lines."""
    candidates = sorted(lines, key=lambda line: math.dist(*line), reverse=True)
    for i, a in enumerate(candidates):
        length_a = math.dist(*a)
        if length_a < minimum_length:
            continue
        da = normalise([a[1][k] - a[0][k] for k in range(3)])
        for b in candidates[i + 1:]:
            length_b = math.dist(*b)
            if abs(length_a - length_b) > max(.25, length_a * 1e-6):
                continue
            db = normalise([b[1][k] - b[0][k] for k in range(3)])
            if abs(sum(x * y for x, y in zip(da, db))) < .999999:
                continue
            if math.dist(a[0], b[1]) < math.dist(a[0], b[0]):
                b = list(reversed(b))
            separation = math.dist(a[0], b[0])
            if not 20 <= separation <= 2000:
                continue
            if expected_width is not None and abs(separation - expected_width) > .25:
                continue
            along = abs(sum((b[0][k] - a[0][k]) * da[k] for k in range(3)))
            if along > .25:
                continue
            return [a[0], a[1], b[1], b[0]], separation
    raise ValueError('No proved pair of longitudinal duct edges')


def insulation_sweep(points, section, thickness_mm, end_section=None, sides=32):
    """Closed annular insulation shell; no overlapping solid core."""
    if not math.isfinite(thickness_mm) or thickness_mm <= 0:
        raise ValueError('Insulation thickness must be finite and positive')
    end_section = end_section or section

    def expanded(value):
        result = dict(value)
        for key in (('diameter_mm',) if value['shape'] == 'round' else ('width_mm', 'height_mm')):
            result[key] += 2 * thickness_mm
        return result

    outer, outer_faces = sweep(points, expanded(section), expanded(end_section), sides)
    inner, inner_faces = sweep(points, section, end_section, sides)
    count = sides if section['shape'] == 'round' else 4
    offset = len(outer)
    faces = outer_faces[:-2] + [[offset + i for i in reversed(face)] for face in inner_faces[:-2]]
    for end in (0, len(points) - 1):
        for j in range(count):
            k = (j + 1) % count
            a, b = end * count + j, end * count + k
            faces.append([a, offset + a, offset + b, b] if end == 0 else [a, b, offset + b, offset + a])
    return outer + inner, faces


def outline_loops(paths, tolerance=.25):
    """Bounded planar face walk, splitting line intersections and pruning stubs.

    This finds drawn faces, not physical duct sections. The caller must select
    the appropriate outline and establish its third dimension independently.
    """
    segments = [(a[:2], b[:2]) for path in paths for a, b in zip(path, path[1:])
                if math.dist(a[:2], b[:2]) > tolerance]
    if len(segments) > 2000:
        raise ValueError('Outline exceeds bounded planar graph budget')
    splits = [[0., 1.] for _ in segments]
    for i, (a, b) in enumerate(segments):
        dx, dy = b[0]-a[0], b[1]-a[1]
        for j in range(i):
            c, d = segments[j]
            ex, ey = d[0]-c[0], d[1]-c[1]
            denominator = dx*ey-dy*ex
            if abs(denominator) < 1e-10:
                continue
            qx, qy = c[0]-a[0], c[1]-a[1]
            t, u = (qx*ey-qy*ex)/denominator, (qx*dy-qy*dx)/denominator
            if -1e-8 <= t <= 1+1e-8 and -1e-8 <= u <= 1+1e-8:
                splits[i].append(max(0., min(1., t)))
                splits[j].append(max(0., min(1., u)))
    vertices, buckets, graph = [], {}, {}

    def vertex(p):
        cell = tuple(math.floor(x/tolerance) for x in p)
        for x in range(cell[0]-1, cell[0]+2):
            for y in range(cell[1]-1, cell[1]+2):
                for index in buckets.get((x, y), []):
                    if math.dist(p, vertices[index]) <= tolerance:
                        return index
        index = len(vertices)
        vertices.append(p)
        buckets.setdefault(cell, []).append(index)
        return index

    for (a, b), values in zip(segments, splits):
        values = sorted(set(values))
        for t, u in zip(values, values[1:]):
            va, vb = [vertex([a[k]+fraction*(b[k]-a[k]) for k in (0, 1)]) for fraction in (t, u)]
            if va != vb:
                graph.setdefault(va, set()).add(vb)
                graph.setdefault(vb, set()).add(va)
    # Open flange strokes cannot bound a face.
    queue = [a for a, neighbours in graph.items() if len(neighbours) < 2]
    while queue:
        a = queue.pop()
        for b in list(graph[a]):
            graph[b].discard(a)
            if len(graph[b]) == 1:
                queue.append(b)
        graph[a].clear()
    ordered = {a: sorted(neighbours, key=lambda b: math.atan2(vertices[b][1]-vertices[a][1],
                                                            vertices[b][0]-vertices[a][0]))
               for a, neighbours in graph.items()}
    visited, loops = set(), []
    for a, neighbours in ordered.items():
        for b in neighbours:
            if (a, b) in visited:
                continue
            walk, start, edge = [], (a, b), (a, b)
            for _ in range(len(segments)*8+1):
                u, v = edge
                if edge in visited:
                    break
                visited.add(edge)
                walk.append(u)
                choices = ordered[v]
                edge = v, choices[(choices.index(u)-1) % len(choices)]
                if edge == start:
                    polygon = [vertices[index]+[0.] for index in walk]
                    area = sum(p[0]*q[1]-q[0]*p[1] for p, q in zip(polygon, polygon[1:]+polygon[:1]))/2
                    if area > tolerance*tolerance and len(set(walk)) == len(walk):
                        loops.append(polygon)
                    break
    return sorted(loops, key=lambda polygon: abs(sum(p[0]*q[1]-q[0]*p[1]
                         for p, q in zip(polygon, polygon[1:]+polygon[:1]))), reverse=True)
