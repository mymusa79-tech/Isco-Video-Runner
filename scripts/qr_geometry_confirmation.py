from __future__ import annotations

from itertools import combinations
from pathlib import Path


# Security V1's Engine-side detector is intentionally cheap: it is a suspicion stage.
# Production confirmation below requires the same 1:1:3:1:1 finder signature on BOTH
# axes plus three mutually-consistent finder centres. This mirrors the shape used by
# mature QR detectors (cross-check before geometric acceptance) without adding a new
# runtime/package dependency to the zero-cost GitHub Actions production path.
_RATIO_TOLERANCE = 0.35
_MODULE_RATIO_MAX = 1.8
_MATCH_RADIUS_MODULES = 1.75
_CLUSTER_RADIUS_MODULES = 2.5
_GEOMETRY_MODULE_RATIO_MAX = 2.0
_PYTHAGOREAN_RELATIVE_TOLERANCE = 0.55
_MAX_SIDE_SQUARED_RATIO = 4.5


def _read_pgm(path: Path) -> tuple[int, int, list[int]]:
    raw = path.read_bytes()
    if not raw.startswith((b"P5", b"P2")):
        raise ValueError("unsupported_image_format")
    i = 2

    def token() -> bytes:
        nonlocal i
        while i < len(raw):
            if raw[i:i + 1] in b" \t\r\n":
                i += 1
                continue
            if raw[i:i + 1] == b"#":
                while i < len(raw) and raw[i:i + 1] not in b"\r\n":
                    i += 1
                continue
            break
        start = i
        while i < len(raw) and raw[i:i + 1] not in b" \t\r\n#":
            i += 1
        if start == i:
            raise ValueError("malformed_pgm")
        return raw[start:i]

    width = int(token())
    height = int(token())
    maxval = int(token())
    if width <= 0 or height <= 0 or maxval <= 0 or maxval > 255:
        raise ValueError("unsupported_pgm")

    if raw.startswith(b"P5"):
        if i >= len(raw) or raw[i:i + 1] not in b" \t\r\n":
            raise ValueError("malformed_pgm")
        i += 1
        pixels = list(raw[i:i + width * height])
        if len(pixels) != width * height:
            raise ValueError("truncated_pgm")
    else:
        pixels = [int(token()) for _ in range(width * height)]
    return width, height, pixels


def _ratio_11311(lengths: tuple[int, int, int, int, int]) -> bool:
    if min(lengths) <= 0:
        return False
    unit = sum(lengths) / 7.0
    expected = (1, 1, 3, 1, 1)
    return all(
        abs(observed - unit * multiplier)
        <= _RATIO_TOLERANCE * unit * multiplier
        for observed, multiplier in zip(lengths, expected)
    )


def _line_hits(values: list[int]) -> list[tuple[float, float]]:
    """Return (centre, module_size) for strict black/white/black/white/black hits."""
    if not values:
        return []
    runs: list[tuple[int, int, int]] = []
    hits: list[tuple[float, float]] = []
    start = 0
    color = values[0]
    for position in range(1, len(values) + 1):
        if position != len(values) and values[position] == color:
            continue
        runs.append((color, position - start, start))
        if len(runs) >= 5:
            recent = runs[-5:]
            colors = tuple(item[0] for item in recent)
            lengths = tuple(item[1] for item in recent)
            if colors == (1, 0, 1, 0, 1) and _ratio_11311(lengths):
                centre = recent[0][2] + lengths[0] + lengths[1] + lengths[2] / 2.0
                hits.append((centre, sum(lengths) / 7.0))
        if position < len(values):
            start = position
            color = values[position]
    return hits


def _axis_hits(
    width: int,
    height: int,
    binary: list[int],
    *,
    horizontal: bool,
) -> list[tuple[float, float, float]]:
    hits: list[tuple[float, float, float]] = []
    if horizontal:
        for y in range(height):
            row = binary[y * width:(y + 1) * width]
            for x, module in _line_hits(row):
                hits.append((x, float(y), module))
        return hits

    for x in range(width):
        column = [binary[y * width + x] for y in range(height)]
        for y, module in _line_hits(column):
            hits.append((float(x), y, module))
    return hits


def _cross_checked_hits(
    horizontal: list[tuple[float, float, float]],
    vertical: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    matched: list[tuple[float, float, float]] = []
    for hx, hy, hm in horizontal:
        for vx, vy, vm in vertical:
            smaller = min(hm, vm)
            larger = max(hm, vm)
            if smaller <= 0 or larger / smaller > _MODULE_RATIO_MAX:
                continue
            radius = max(2.0, _MATCH_RADIUS_MODULES * larger)
            if abs(hx - vx) > radius or abs(hy - vy) > radius:
                continue
            matched.append(((hx + vx) / 2.0, (hy + vy) / 2.0, (hm + vm) / 2.0))
    return matched


def _cluster_finder_centres(
    points: list[tuple[float, float, float]],
) -> list[tuple[float, float, float, int]]:
    clusters: list[list[tuple[float, float, float]]] = []
    for point in points:
        px, py, pm = point
        for cluster in clusters:
            cx = sum(item[0] for item in cluster) / len(cluster)
            cy = sum(item[1] for item in cluster) / len(cluster)
            cm = sum(item[2] for item in cluster) / len(cluster)
            radius = max(3.0, _CLUSTER_RADIUS_MODULES * max(pm, cm))
            if abs(px - cx) <= radius and abs(py - cy) <= radius:
                cluster.append(point)
                break
        else:
            clusters.append([point])

    centres: list[tuple[float, float, float, int]] = []
    for cluster in clusters:
        # A real finder is normally observed on several adjacent rows/columns. One
        # accidental cross-match is not enough to obtain blocking authority.
        if len(cluster) < 2:
            continue
        centres.append(
            (
                sum(item[0] for item in cluster) / len(cluster),
                sum(item[1] for item in cluster) / len(cluster),
                sum(item[2] for item in cluster) / len(cluster),
                len(cluster),
            )
        )
    return centres


def _three_finders_form_qr_geometry(
    centres: list[tuple[float, float, float, int]],
) -> bool:
    for first, second, third in combinations(centres, 3):
        modules = (first[2], second[2], third[2])
        smallest_module = min(modules)
        largest_module = max(modules)
        if smallest_module <= 0 or largest_module / smallest_module > _GEOMETRY_MODULE_RATIO_MAX:
            continue

        points = ((first[0], first[1]), (second[0], second[1]), (third[0], third[1]))
        distances = []
        for a, b in combinations(points, 2):
            dx = a[0] - b[0]
            dy = a[1] - b[1]
            distances.append(dx * dx + dy * dy)
        short, middle, long = sorted(distances)
        min_separation = max(12.0, 6.0 * largest_module)
        if short < min_separation * min_separation:
            continue
        if long / short > _MAX_SIDE_SQUARED_RATIO:
            continue
        legs = short + middle
        if legs <= 0 or abs(long - legs) / legs > _PYTHAGOREAN_RELATIVE_TOLERANCE:
            continue

        ax, ay = points[0]
        bx, by = points[1]
        cx, cy = points[2]
        doubled_area = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))
        if doubled_area < 0.18 * long:
            continue
        return True
    return False


def confirm_qr_finder_geometry(path: str | Path) -> bool:
    """Confirm an Engine QR suspicion using two-axis finder + triangle geometry.

    This function NEVER grants a media pass. It only decides whether the cheap QR
    suspicion has enough independent geometry to retain the `qr_code_detected` block.
    All other Security V1 detections remain authoritative and unchanged.

    Any parser/confirmation error fails closed by returning True: inability to perform
    the confirmation must never silently downgrade an Engine security finding.
    """
    try:
        width, height, pixels = _read_pgm(Path(path))
        binary = [1 if pixel < 128 else 0 for pixel in pixels]
        horizontal = _axis_hits(width, height, binary, horizontal=True)
        if not horizontal:
            return False
        vertical = _axis_hits(width, height, binary, horizontal=False)
        if not vertical:
            return False
        matched = _cross_checked_hits(horizontal, vertical)
        centres = _cluster_finder_centres(matched)
        return _three_finders_form_qr_geometry(centres)
    except Exception:
        return True
