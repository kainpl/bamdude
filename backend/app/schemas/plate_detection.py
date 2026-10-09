"""Normalized, simple camera contours; independent of OpenCV availability."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


class PlatePoint(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


def validate_polygon(points: list[PlatePoint]) -> list[PlatePoint]:
    xy = [(p.x, p.y) for p in points]
    if len(set(xy)) != len(xy):
        raise ValueError("Polygon vertices must be distinct")
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(xy, xy[1:] + xy[:1], strict=True))) / 2
    if area < 0.00001:
        raise ValueError("Polygon must have a non-zero area")

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a, b, p):
        return abs(cross(a, b, p)) < 1e-12 and all(min(a[i], b[i]) <= p[i] <= max(a[i], b[i]) for i in (0, 1))

    edges = list(zip(xy, xy[1:] + xy[:1], strict=True))
    for i, (a, b) in enumerate(edges):
        for j, (c, d) in enumerate(edges):
            if j <= i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            if (cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0) or any(
                (on_segment(a, b, c), on_segment(a, b, d), on_segment(c, d, a), on_segment(c, d, b))
            ):
                raise ValueError("Polygon edges must not intersect")
    return points


PlatePolygon = Annotated[list[PlatePoint], Field(min_length=3, max_length=32), AfterValidator(validate_polygon)]
