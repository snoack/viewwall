from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .config import LayoutConfig, RectSpec, ViewportConfig


@dataclass(frozen=True)
class Insets:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0


@dataclass(frozen=True)
class PixelRect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class ResolvedViewport:
    name: str
    full: PixelRect
    render: PixelRect
    insets: Insets


@dataclass(frozen=True)
class SourceCrop:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width_removed(self) -> int:
        return self.left + self.right

    @property
    def height_removed(self) -> int:
        return self.top + self.bottom


def _round_edge(value: Fraction, extent: int) -> int:
    scaled = value * extent
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder * 2 >= scaled.denominator:
        quotient += 1
    return quotient


def _base_rect(spec: RectSpec, width: int, height: int) -> PixelRect:
    x0 = _round_edge(spec.x, width)
    y0 = _round_edge(spec.y, height)
    x1 = _round_edge(spec.x + spec.width, width)
    y1 = _round_edge(spec.y + spec.height, height)
    return PixelRect(x=x0, y=y0, width=x1 - x0, height=y1 - y0)


def resolve_viewport(viewport: ViewportConfig, output_width: int, output_height: int, layout: LayoutConfig) -> ResolvedViewport:
    if output_width <= 0 or output_height <= 0:
        raise ValueError("output dimensions must be positive")
    full = _base_rect(viewport.rect, output_width, output_height)
    outer = layout.outer_margin_px
    gap = layout.gap_px
    at_left = viewport.rect.x == 0
    at_top = viewport.rect.y == 0
    at_right = viewport.rect.x + viewport.rect.width == 1
    at_bottom = viewport.rect.y + viewport.rect.height == 1
    insets = Insets(
        left=outer if at_left else 0,
        top=outer if at_top else 0,
        right=outer if at_right else gap,
        bottom=outer if at_bottom else gap,
    )
    render = PixelRect(
        x=full.x + insets.left,
        y=full.y + insets.top,
        width=full.width - insets.left - insets.right,
        height=full.height - insets.top - insets.bottom,
    )
    if render.width <= 0 or render.height <= 0:
        raise ValueError(f"viewport {viewport.name} is too small for the configured gap/margin")
    return ResolvedViewport(name=viewport.name, full=full, render=render, insets=insets)


def resolve_layout(
    viewports: tuple[ViewportConfig, ...], output_width: int, output_height: int, layout: LayoutConfig
) -> dict[str, ResolvedViewport]:
    return {viewport.name: resolve_viewport(viewport, output_width, output_height, layout) for viewport in viewports}


def source_crop(source_width: int, source_height: int, viewport: ResolvedViewport) -> SourceCrop:
    """Clip source-edge pixels instead of squeezing a feed into its seam."""
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source dimensions must be positive")
    crop = SourceCrop(
        left=viewport.insets.left,
        top=viewport.insets.top,
        right=viewport.insets.right,
        bottom=viewport.insets.bottom,
    )
    if crop.width_removed >= source_width or crop.height_removed >= source_height:
        raise ValueError(f"source is too small for viewport {viewport.name} crop")
    return crop
