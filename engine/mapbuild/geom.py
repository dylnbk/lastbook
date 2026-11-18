"""The little geometry a region build needs, on plain lists: no library to install.

A multipolygon here is GeoJSON's: [polygon, ...], each polygon [ring, ...] (outer first, then holes), each ring [[lon, lat], ...].
"""
import math


def multipolygon(geometry):
    """A GeoJSON geometry -> multipolygon coordinates, or []."""
    if not geometry:
        return []
    if geometry.get('type') == 'Polygon':
        return [geometry['coordinates']]
    if geometry.get('type') == 'MultiPolygon':
        return geometry['coordinates']
    return []


def box_of(mp):
    xs = [p[0] for poly in mp for ring in poly for p in ring]
    ys = [p[1] for poly in mp for ring in poly for p in ring]
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def inside(mp, lon, lat):
    """Even-odd: a point in an outer ring and not in a hole of it."""
    for poly in mp:
        hit = False
        for ring in poly:
            n = len(ring)
            j = n - 1
            for i in range(n):
                xi, yi = ring[i][0], ring[i][1]
                xj, yj = ring[j][0], ring[j][1]
                if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                    hit = not hit
                j = i
        if hit:
            return True
    return False


def cells(mp, size):
    """Every size-degree cell the multipolygon touches, as a set of (column, row) counted from -180, -90.
    Rows are filled between ring crossings at each row's middle; then each edge is walked so thin slivers and edges count too."""
    out = set()
    for poly in mp:
        ys = [p[1] for ring in poly for p in ring]
        if not ys:
            continue
        r0, r1 = int((min(ys) + 90) / size), int((max(ys) + 90) / size)
        for r in range(r0, r1 + 1):
            lat = (r + 0.5) * size - 90
            xs = []
            for ring in poly:
                n = len(ring)
                j = n - 1
                for i in range(n):
                    xi, yi = ring[i][0], ring[i][1]
                    xj, yj = ring[j][0], ring[j][1]
                    if (yi > lat) != (yj > lat):
                        xs.append((xj - xi) * (lat - yi) / (yj - yi) + xi)
                    j = i
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                for c in range(int((xs[k] + 180) / size), int((xs[k + 1] + 180) / size) + 1):
                    out.add((c, r))
        step = size / 2
        for ring in poly:
            for i in range(len(ring)):
                ax, ay = ring[i - 1][0], ring[i - 1][1]
                bx, by = ring[i][0], ring[i][1]
                n = max(1, int(math.ceil(max(abs(bx - ax), abs(by - ay)) / step)))
                for k in range(n + 1):
                    t = k / n
                    out.add((int((ax + (bx - ax) * t + 180) / size), int((ay + (by - ay) * t + 90) / size)))
    return out


def box_cells(minx, miny, maxx, maxy, size):
    for c in range(int((minx + 180) / size), int((maxx + 180) / size) + 1):
        for r in range(int((miny + 90) / size), int((maxy + 90) / size) + 1):
            yield (c, r)


def simplify_ring(ring, tol):
    """Douglas-Peucker on [[lon, lat], ...] (a region outline is a few hundred points; recursion is not needed for more)."""
    if len(ring) < 5 or tol <= 0:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = ring[a][0], ring[a][1]
        bx, by = ring[b][0], ring[b][1]
        dx, dy = bx - ax, by - ay
        d2 = dx * dx + dy * dy
        worst, at = 0.0, -1
        for i in range(a + 1, b):
            px, py = ring[i][0] - ax, ring[i][1] - ay
            if d2:
                t = max(0.0, min(1.0, (px * dx + py * dy) / d2))
                ex, ey = px - t * dx, py - t * dy
            else:
                ex, ey = px, py
            e = ex * ex + ey * ey
            if e > worst:
                worst, at = e, i
        if at >= 0 and worst > tol * tol:
            keep[at] = True
            stack.append((a, at))
            stack.append((at, b))
    return [p for p, k in zip(ring, keep) if k]
