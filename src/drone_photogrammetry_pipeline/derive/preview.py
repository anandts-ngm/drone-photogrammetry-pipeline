"""Small viewable renderings of a master, and a contact sheet of a whole project.

Derived products. They are lossy and resampled, both of which the master contract forbids for
a master, and both of which are what makes a preview useful: a 1.4-gigapixel raster nobody can
open is not a way to look at a survey.

JPEG on white rather than PNG or WebP with alpha, because the point is that anyone can open it
without thinking about it, and a block's irregular footprint reads clearly against white.

Masters carry no overviews by contract, so rendering one means reading it in full. That is the
cost of the contract choice, paid here once per block rather than on every pan in a viewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw
from rasterio.enums import Resampling

from .destripe import DestripeResult, destripe

DEFAULT_LONGEST_SIDE = 2048
DEFAULT_QUALITY = 85
THUMBNAIL = 320
LABEL_HEIGHT = 18

# Straight alpha over white. The master contract requires NON-PREMULTIPLIED alpha, so this is
# a plain interpolation; on premultiplied data the same arithmetic would darken every edge.
_WHITE = 255.0


class PreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class Preview:
    path: Path
    width: int
    height: int
    bytes_written: int


def _fit(width: int, height: int, longest: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise PreviewError(f"cannot render a {width}x{height} raster")
    if width >= height:
        return longest, max(1, round(longest * height / width))
    return max(1, round(longest * width / height)), longest


def composite_on_white(rgb: NDArray[np.number], alpha: NDArray[np.number]) -> NDArray[np.uint8]:
    """Flatten RGBA onto white, so invalid ground reads as background rather than black."""
    coverage = (np.asarray(alpha, dtype=np.float32) / 255.0)[None, :, :]
    flattened = np.asarray(rgb, dtype=np.float32) * coverage + _WHITE * (1.0 - coverage)
    return np.clip(flattened, 0, 255).astype(np.uint8)


def render(
    master: Path,
    *,
    longest_side: int = DEFAULT_LONGEST_SIDE,
    apply_destripe: bool = False,
) -> tuple[Image.Image, DestripeResult | None]:
    """Read one master down to `longest_side` and flatten it onto white.

    Destriping is offered here and refused for masters: a preview is already lossy and
    resampled and nothing is measured from it, so removing Terra's flight-strip banding costs
    nothing that the master does not still hold exactly.
    """
    import rasterio

    with rasterio.open(master) as ds:
        if ds.count < 4:
            raise PreviewError(f"{master.name} has {ds.count} bands; a master carries 4")
        width, height = _fit(int(ds.width), int(ds.height), longest_side)
        rgb = ds.read([1, 2, 3], out_shape=(3, height, width), resampling=Resampling.average)
        alpha = ds.read(4, out_shape=(height, width), resampling=Resampling.average)

    result: DestripeResult | None = None
    if apply_destripe:
        rgb, result = destripe(rgb, alpha)

    flattened = composite_on_white(rgb, alpha)
    return Image.fromarray(np.ascontiguousarray(flattened.transpose(1, 2, 0))), result


def write_preview(
    master: Path,
    destination: Path,
    *,
    longest_side: int = DEFAULT_LONGEST_SIDE,
    quality: int = DEFAULT_QUALITY,
    apply_destripe: bool = False,
) -> tuple[Preview, Image.Image, DestripeResult | None]:
    """Write one preview, returning the record and the rendered image for reuse in a sheet."""
    image, result = render(master, longest_side=longest_side, apply_destripe=apply_destripe)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "JPEG", quality=quality, optimize=True, progressive=True)
    return (
        Preview(
            path=destination,
            width=image.width,
            height=image.height,
            bytes_written=destination.stat().st_size,
        ),
        image,
        result,
    )


def write_contact_sheet(
    rendered: list[tuple[str, Image.Image]],
    destination: Path,
    *,
    columns: int = 10,
    thumbnail: int = THUMBNAIL,
) -> Preview:
    """One page showing every block, labelled.

    The reason this exists rather than a folder of files: block-to-block consistency is the
    property most worth looking at, and it is invisible when the blocks are viewed one at a
    time.
    """
    if not rendered:
        raise PreviewError("no rendered blocks; a contact sheet of nothing is not a product")

    columns = max(1, min(columns, len(rendered)))
    rows = -(-len(rendered) // columns)
    sheet = Image.new(
        "RGB", (columns * thumbnail, rows * (thumbnail + LABEL_HEIGHT)), (245, 245, 245)
    )
    draw = ImageDraw.Draw(sheet)

    for index, (label, image) in enumerate(rendered):
        thumb = image.copy()
        thumb.thumbnail((thumbnail, thumbnail), Image.Resampling.LANCZOS)
        column, row = index % columns, index // columns
        sheet.paste(
            thumb,
            (
                column * thumbnail + (thumbnail - thumb.width) // 2,
                row * (thumbnail + LABEL_HEIGHT) + LABEL_HEIGHT,
            ),
        )
        draw.text(
            (column * thumbnail + 4, row * (thumbnail + LABEL_HEIGHT) + 3),
            label,
            fill=(40, 40, 40),
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, "JPEG", quality=88, optimize=True)
    return Preview(
        path=destination,
        width=sheet.width,
        height=sheet.height,
        bytes_written=destination.stat().st_size,
    )
