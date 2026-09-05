from fractions import Fraction

from viewwall.config import LayoutConfig, RectSpec, ViewportConfig
from viewwall.layout import resolve_layout, source_crop


def viewport(index: int, x: str, y: str, width: str, height: str) -> ViewportConfig:
    return ViewportConfig(
        index=index,
        rect=RectSpec(Fraction(x), Fraction(y), Fraction(width), Fraction(height)),
        feeds=(f"feed{index}",),
    )


def grid() -> tuple[ViewportConfig, ...]:
    return tuple(
        viewport(row * 3 + column + 1, f"{column}/3", f"{row}/3", "1/3", "1/3")
        for row in range(3)
        for column in range(3)
    )


def test_three_by_three_1080p_has_one_pixel_seams() -> None:
    result = resolve_layout(grid(), 1920, 1080, LayoutConfig(gap_px=1))
    upper_left = result["viewport1"]
    center = result["viewport5"]
    lower_right = result["viewport9"]
    assert upper_left.full.width == 640
    assert upper_left.full.height == 360
    assert (upper_left.render.width, upper_left.render.height) == (639, 359)
    assert (center.render.x, center.render.y) == (640, 360)
    assert (center.render.width, center.render.height) == (639, 359)
    assert (lower_right.render.width, lower_right.render.height) == (640, 360)


def test_edges_are_shared_at_non_divisible_resolution() -> None:
    result = resolve_layout(grid(), 1366, 768, LayoutConfig(gap_px=0))
    row = [result[f"viewport{column + 1}"].full for column in range(3)]
    assert row[0].x + row[0].width == row[1].x
    assert row[1].x + row[1].width == row[2].x
    assert row[2].x + row[2].width == 1366


def test_source_is_cropped_instead_of_squeezed() -> None:
    result = resolve_layout((grid()[0],), 1920, 1080, LayoutConfig(gap_px=1))["viewport1"]
    crop = source_crop(640, 360, result)
    assert crop.left == 0
    assert crop.top == 0
    assert crop.right == 1
    assert crop.bottom == 1


def test_outer_margin_crops_all_display_edges() -> None:
    full = viewport(1, "0", "0", "1", "1")
    result = resolve_layout((full,), 800, 600, LayoutConfig(outer_margin_px=2))["viewport1"]
    assert (result.render.x, result.render.y) == (2, 2)
    assert (result.render.width, result.render.height) == (796, 596)

