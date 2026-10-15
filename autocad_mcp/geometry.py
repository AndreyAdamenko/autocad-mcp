"""Coordinate algebra and tolerance-controlled sampling, independent of COM/bpy.

Matrices use column vectors. Entity data stay in definition drawing units;
world_matrix maps them directly into WCS millimetres, including nested inserts.
"""
from __future__ import annotations

import math
import bisect


def identity():
    return [[float(i == j) for j in range(4)] for i in range(4)]


def multiply(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def transform(matrix, p):
    return [sum(matrix[i][j] * p[j] for j in range(3)) + matrix[i][3] for i in range(3)]


def vector(matrix, p):
    return [sum(matrix[i][j] * p[j] for j in range(3)) for i in range(3)]


def cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def norm(v):
    return math.sqrt(sum(x*x for x in v))


def normalise(v):
    n = norm(v)
    if n < 1e-15:
        raise ValueError("zero coordinate-system normal")
    return [x/n for x in v]


def ocs_basis(normal):
    # Autodesk arbitrary axis algorithm, used only for OCS fields.
    z = normalise(normal)
    x = normalise(cross([0, 1, 0] if abs(z[0]) < 1/64 and abs(z[1]) < 1/64 else [0, 0, 1], z))
    y = cross(z, x)
    m = identity()
    for i in range(3):
        m[i][:3] = [x[i], y[i], z[i]]
    return m


def insertion_matrix(point, normal, rotation, scales, origin):
    b = ocs_basis(normal)
    c, s = math.cos(rotation), math.sin(rotation)
    r = [[c*scales[0], -s*scales[1], 0, 0],
         [s*scales[0], c*scales[1], 0, 0],
         [0, 0, scales[2], 0], [0, 0, 0, 1]]
    m = multiply(b, r)
    offset = vector(m, origin)
    for i in range(3):
        m[i][3] = point[i] - offset[i]
    return m


def bounds(points):
    if not points:
        return None
    return [[min(p[i] for p in points) for i in range(3)],
            [max(p[i] for p in points) for i in range(3)]]


def arc_points(center, u, v, start, sweep, tolerance, max_segments=100000):
    radius = max(norm(u), norm(v))
    if radius == 0:
        raise ValueError("degenerate curve")
    step = 2*math.acos(max(-1., min(1., 1-tolerance/radius))) if tolerance < radius else math.pi/2
    count = max(2, math.ceil(abs(sweep)/max(step, 1e-8)))
    if count > max_segments:
        raise ValueError("curve requires too many segments at requested tolerance")
    return [[center[k] + u[k]*math.cos(start+sweep*i/count) + v[k]*math.sin(start+sweep*i/count)
             for k in range(3)] for i in range(count+1)]


def sample_geometry(g, tolerance=0.25):
    """Return local points and topology; exact curve parameters remain in records."""
    kind = g["kind"]
    if kind == "spline":
        pts = sample_spline(g, tolerance)
        closed = g["closed"]
        if closed and norm([pts[-1][i]-pts[0][i] for i in range(3)]) < 1e-8:
            pts.pop()
        return pts, [[i,i+1] for i in range(len(pts)-1)] + ([[len(pts)-1,0]] if closed else []), [], closed
    if kind == "line":
        return [g["start"], g["end"]], [[0, 1]], [], False
    if kind == "point":
        return [g["point"]], [], [], False
    if kind in ("face", "mesh"):
        return g["vertices"], [], g["faces"], False
    if kind == "polyline":
        pts, closed = g["vertices"], g["closed"]
        samples = []
        basis = ocs_basis(g.get("normal", [0, 0, 1]))
        normal = [basis[i][2] for i in range(3)]
        bulges = g.get("bulges") or [0]*len(pts)
        for i in range(len(pts) if closed else len(pts)-1):
            a, b, bulge = pts[i], pts[(i+1) % len(pts)], bulges[i]
            if abs(bulge) < 1e-14:
                part = [a, b]
            else:
                delta = [b[k]-a[k] for k in range(3)]
                chord = norm(delta)
                if chord < 1e-15:
                    # A repeated vertex contributes zero arc length even when
                    # its stored bulge is nonzero. Retain raw vertices in COM
                    # provenance; do not normalise a zero chord.
                    samples.extend([b] if samples else [a,b])
                    continue
                side = normalise(cross(normal, delta))
                center = [(a[k]+b[k])/2 + side[k]*chord*(1-bulge*bulge)/(4*bulge) for k in range(3)]
                u = [a[k]-center[k] for k in range(3)]
                v = cross(normal, u)
                part = arc_points(center, u, v, 0, 4*math.atan(bulge), tolerance)
            samples.extend(part if not samples else part[1:])
        if closed and samples:
            samples.pop()  # endpoint coincides with first
        return samples, [[i, i+1] for i in range(len(samples)-1)] + ([[len(samples)-1, 0]] if closed else []), [], closed
    if kind in ("circle", "arc", "ellipse"):
        if kind == "ellipse":
            u = g["major_axis"]
            v = [x*g["ratio"] for x in cross(normalise(g["normal"]), u)]
            start, sweep = g["start_parameter"], g["sweep_parameter"]
            closed = abs(sweep-2*math.pi) < 1e-8
        else:
            b = ocs_basis(g["normal"])
            u, v = [[b[i][j]*g["radius"] for i in range(3)] for j in (0, 1)]
            start, sweep = (0, 2*math.pi) if kind == "circle" else (g["start_angle"], g["sweep"])
            closed = kind == "circle"
        pts = arc_points(g["center"], u, v, start, sweep, tolerance)
        if closed:
            pts.pop()
        return pts, [[i, i+1] for i in range(len(pts)-1)] + ([[len(pts)-1, 0]] if closed else []), [], closed
    raise ValueError(f"No verified tessellation for {kind}")


def polyline_length(g):
    pts, closed = g["vertices"], g["closed"]
    bulges = g.get("bulges") or [0]*len(pts)
    total = 0
    for i in range(len(pts) if closed else len(pts)-1):
        chord = norm([pts[(i+1) % len(pts)][j]-pts[i][j] for j in range(3)])
        b = bulges[i]
        total += chord if abs(b)<1e-14 else chord*(1+b*b)/abs(4*b)*abs(4*math.atan(b))
    return total


def world_geometry(record, tolerance_mm=0.25):
    matrix = record["world_matrix"]
    # Frobenius norm bounds the largest singular value, also under shear.
    amplification = math.sqrt(sum(matrix[i][j]**2 for i in range(3) for j in range(3)))
    pts, edges, faces, closed = sample_geometry(record["geometry"], tolerance_mm/max(amplification, 1e-15))
    return [transform(matrix, p) for p in pts], edges, faces, closed


def nurbs_point(g, t):
    cp, knots, weights, degree = g["control_points"], g["knots"], g["weights"], g["degree"]
    n = len(cp)
    if len(knots) != n+degree+1 or len(weights) != n or not 1 <= degree < n:
        raise ValueError("Invalid or unsupported NURBS knot/control-point structure")
    if any(a>b for a,b in zip(knots,knots[1:])) or any(w<=0 for w in weights):
        raise ValueError("Unsupported NURBS knot or weight values")
    span = min(n-1, max(degree, bisect.bisect_right(knots,t)-1))
    work = [[v*weights[i] for v in cp[i]]+[weights[i]] for i in range(span-degree,span+1)]
    for r in range(1,degree+1):
        for j in range(degree,r-1,-1):
            i = span-degree+j
            delta = knots[i+degree-r+1]-knots[i]
            alpha = (t-knots[i])/delta if delta else 0
            work[j] = [(1-alpha)*a+alpha*b for a,b in zip(work[j-1],work[j])]
    value = work[degree]
    return [x/value[3] for x in value[:3]]


def sample_spline(g, tolerance):
    # Adaptive evaluation on every nonempty knot span. The source NURBS stays
    # exact in provenance; this preview's numerical tolerance is not a proof
    # of a global Hausdorff bound and is reported as such.
    cp, degree, knots = g["control_points"], g["degree"], g["knots"]
    nurbs_point(g, knots[degree])  # validate structure before slicing
    parameters = sorted(set(knots[degree:len(cp)+1]))
    result = []
    def visit(a, b, pa, pb, depth):
        mids = [a+(b-a)*f for f in (0.25,0.5,0.75)]
        actual = [nurbs_point(g,t) for t in mids]
        error = max(norm([p[i]-(pa[i]+(pb[i]-pa[i])*f) for i in range(3)])
                    for p,f in zip(actual,(0.25,0.5,0.75)))
        if error <= tolerance:
            result.append(pb)
        elif depth >= 24 or len(result) > 100000:
            raise ValueError("Spline sampling could not meet requested tolerance")
        else:
            visit(a,mids[1],pa,actual[1],depth+1)
            visit(mids[1],b,actual[1],pb,depth+1)
    for a,b in zip(parameters,parameters[1:]):
        pa,pb = nurbs_point(g,a),nurbs_point(g,b)
        if not result:
            result.append(pa)
        visit(a,b,pa,pb,0)
    return result
