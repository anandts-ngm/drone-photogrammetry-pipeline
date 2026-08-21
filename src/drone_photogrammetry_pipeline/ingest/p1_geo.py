"""ODM geolocation for a DJI P1 flight folder.

A P1 block arrives as raw imagery: the JPGs, a `Timestamp.MRK` event-mark file, the PPK
observables, and (when the drone team has run exiftool over the card) a `metadata.csv`. This
module reads those and checks them against each other before anything is submitted for
reconstruction.

**It does not write a `geo.txt` by default, and that is the finding rather than an omission.**
ODM already reads `@drone-dji:RtkStdLon/RtkStdLat/RtkStdHgt` from each image's XMP and weights
that image's position by them; on this flight that is 1.3 cm to 2.3 cm. A geo file replaces
those values with whatever it carries, and its accuracy columns sit behind `yaw pitch roll`,
which cannot be written correctly from here: ODM takes yaw from `FlightYawDegree` (absent from
the exiftool export) and stores `90 + GimbalPitchDegree` for a DJI make on the EXIF path only.
So a sidecar written from this data would discard a 2 cm weighting in favour of ODM's 3 m
default. Reading the mark file is still worth doing — for the checks below — and writing the
file stays available for the one case that needs it, the lever-arm experiment.

Three properties this has to have, and why:

* **The MRK covers the flight; the folder covers a block.** The measured folder holds 79 JPGs
  and its MRK 649 exposures, because DJI writes one mark file per flight and the imagery is
  split across folders. Every line is read, and only the images actually present are used.
* **The image-to-exposure match is verified, not assumed.** The four-digit suffix in
  `DJI_20260803132556_0001.JPG` is taken as the exposure number, and then checked against the
  EXIF position of the same image: they must agree to within a metre or the match is refused.
  A silently wrong match would georeference every image to a neighbour's position.
* **Nothing is written into the source folder.** Anything generated goes to the workspace and
  travels to ODM as an upload, because a source tree stays exactly as it was delivered.

The antenna-to-camera lever arm is **not** applied; see `write_geo_file` and
`docs/decisions-and-verification.md` §2.16 for the measurement and the open question.
"""

from __future__ import annotations

import csv
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

# The name ODM looks for. It has to be exactly this, alongside the imagery.
GEO_FILENAME = "geo.txt"
DEFAULT_SRS = "EPSG:4326"

# Metres per degree, near enough for a residual check and for a lever arm of under a metre.
_METRES_PER_DEGREE_LAT = 111_132.0
_METRES_PER_DEGREE_LON = 111_320.0

# A wrong image-to-exposure match displaces a position by the flight-line spacing, tens of
# metres. A right one differs only by EXIF's DMS quantisation, millimetres. Anything between is
# a mismatch worth stopping for.
POSITION_TOLERANCE_M = 1.0

# How far off straight down a gimbal may be and still count as a mapping exposure.
NADIR_TOLERANCE_DEG = 1.0

_EXPOSURE_INDEX = re.compile(r"_(\d{4})\b")

# `   402,N` and `45.95639119,Lat`: value first, label second, spacing irregular.
_LABELLED = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*,\s*(N|E|V|Lat|Lon|Ellh|Q)\b")
_FLOAT = re.compile(r"[-+]?\d+(?:\.\d+)?")


class P1GeoError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarkRecord:
    """One exposure event, as the aircraft recorded it."""

    exposure: int
    latitude: float
    longitude: float
    ellipsoidal_height: float

    # Standard deviations of the position solution, in metres, north / east / up.
    std_north: float
    std_east: float
    std_up: float

    # Antenna phase centre to camera offsets, in metres, in the local ENU frame at exposure.
    offset_north: float
    offset_east: float
    offset_up: float

    # DJI's positioning-quality flag, carried through verbatim rather than interpreted: the
    # documented values are 0, 16, 34 and 50, and this delivery reports 52.
    flag: str

    @property
    def horizontal_accuracy(self) -> float:
        return math.hypot(self.std_north, self.std_east)


@dataclass(frozen=True)
class ExifRecord:
    """The fields of one exiftool row that bear on geolocation."""

    filename: str
    latitude: float
    longitude: float
    absolute_altitude: float
    gimbal_yaw: float | None
    gimbal_pitch: float | None
    gimbal_roll: float | None


@dataclass(frozen=True)
class Exposure:
    """One image, matched to its exposure event."""

    path: Path
    mark: MarkRecord
    exif: ExifRecord | None

    @property
    def has_attitude(self) -> bool:
        return self.exif is not None and self.exif.gimbal_pitch is not None

    @property
    def is_nadir(self) -> bool:
        """Whether the gimbal was pointing down, within a degree.

        Reported rather than enforced. An oblique block is a legitimate acquisition, but it is
        not a mapping block, and finding that out from the reconstruction rather than before
        submitting it costs hours.
        """
        pitch = self.exif.gimbal_pitch if self.exif else None
        return pitch is not None and abs(pitch + 90.0) <= NADIR_TOLERANCE_DEG


@dataclass(frozen=True)
class BlockGeolocation:
    """What the mark file says about the images that are present.

    The accuracies are 95th percentiles rather than medians, because the number a caller does
    anything with is `--gps-accuracy`, which is one value for the whole block: understating it
    would let the bundle adjustment hold the worst positions more tightly than they deserve.

    `nadir_within_tolerance` counts images whose gimbal pitch was within a degree of straight
    down. It is counted separately from `with_attitude` because a block with no attitude at all
    and a genuinely oblique block are different things that both report zero nadir images.
    """

    images: int
    with_attitude: int
    nadir_within_tolerance: int
    horizontal_accuracy_m: float
    vertical_accuracy_m: float
    flags: tuple[str, ...]
    lever_arm: LeverArm

    @property
    def attitude_known(self) -> bool:
        return self.with_attitude > 0


@dataclass(frozen=True)
class GeoFile:
    """The sidecar that was written, if one was."""

    path: Path
    srs: str
    images: int
    lever_arm_applied: bool


def read_mark_file(path: Path) -> dict[int, MarkRecord]:
    """Parse a DJI `Timestamp.MRK`, keyed by exposure number."""
    records: dict[int, MarkRecord] = {}
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        if not line.strip():
            continue
        labelled = {label: float(value) for value, label in _LABELLED.findall(line)}
        missing = {"N", "E", "V", "Lat", "Lon", "Ellh"} - labelled.keys()
        if missing:
            raise P1GeoError(f"{path.name} line {number} has no {sorted(missing)} field")

        exposure_match = _FLOAT.match(line.strip())
        if exposure_match is None:
            raise P1GeoError(f"{path.name} line {number} does not start with an exposure number")

        # The three standard deviations are the only unlabelled numbers, and they sit between
        # the height and the quality flag.
        ellh_end = line.index("Ellh") + len("Ellh")
        flag_start = line.rindex(",")
        deviations = _FLOAT.findall(line[ellh_end:flag_start])
        if len(deviations) < 3:
            raise P1GeoError(
                f"{path.name} line {number} carries {len(deviations)} standard deviations, "
                "expected three (north, east, up)"
            )

        exposure = int(float(exposure_match.group()))
        records[exposure] = MarkRecord(
            exposure=exposure,
            latitude=labelled["Lat"],
            longitude=labelled["Lon"],
            ellipsoidal_height=labelled["Ellh"],
            std_north=float(deviations[0]),
            std_east=float(deviations[1]),
            std_up=float(deviations[2]),
            offset_north=labelled["N"] / 1000.0,
            offset_east=labelled["E"] / 1000.0,
            offset_up=labelled["V"] / 1000.0,
            flag=str(int(labelled.get("Q", 0))),
        )

    if not records:
        raise P1GeoError(f"{path} holds no exposure events")
    return records


def _float_or_none(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def read_metadata_csv(path: Path) -> dict[str, ExifRecord]:
    """Parse an exiftool CSV export, keyed by image filename.

    Optional by design: DJI does not write this file, the drone team does. Without it the
    positions are still known, and only the attitude and the cross-check are lost.
    """
    records: dict[str, ExifRecord] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("FileName") or "").strip()
            latitude = _float_or_none(row.get("GPSLatitude"))
            longitude = _float_or_none(row.get("GPSLongitude"))
            if not name or latitude is None or longitude is None:
                continue
            altitude = _float_or_none(row.get("AbsoluteAltitude"))
            records[name] = ExifRecord(
                filename=name,
                latitude=latitude,
                longitude=longitude,
                absolute_altitude=altitude if altitude is not None else 0.0,
                gimbal_yaw=_float_or_none(row.get("GimbalYawDegree")),
                gimbal_pitch=_float_or_none(row.get("GimbalPitchDegree")),
                gimbal_roll=_float_or_none(row.get("GimbalRollDegree")),
            )
    if not records:
        raise P1GeoError(f"{path} holds no rows with a filename and a GPS position")
    return records


def exposure_number(image: Path) -> int | None:
    """The exposure number DJI put in the filename, or None if the name does not carry one."""
    matches = _EXPOSURE_INDEX.findall(image.stem)
    return int(matches[-1]) if matches else None


def _separation_metres(mark: MarkRecord, exif: ExifRecord) -> float:
    north = (exif.latitude - mark.latitude) * _METRES_PER_DEGREE_LAT
    east = (
        (exif.longitude - mark.longitude)
        * _METRES_PER_DEGREE_LON
        * math.cos(math.radians(mark.latitude))
    )
    return math.hypot(north, east)


def match_exposures(
    images: list[Path],
    marks: dict[int, MarkRecord],
    exif: dict[str, ExifRecord] | None = None,
    *,
    tolerance_m: float = POSITION_TOLERANCE_M,
) -> list[Exposure]:
    """Pair every image with its exposure event, refusing a match the EXIF contradicts."""
    matched: list[Exposure] = []
    unnumbered: list[str] = []
    unmatched: list[str] = []
    disagreeing: list[str] = []

    for image in sorted(images):
        number = exposure_number(image)
        if number is None:
            unnumbered.append(image.name)
            continue
        mark = marks.get(number)
        if mark is None:
            unmatched.append(image.name)
            continue
        row = exif.get(image.name) if exif else None
        if row is not None:
            separation = _separation_metres(mark, row)
            if separation > tolerance_m:
                disagreeing.append(f"{image.name} ({separation:.1f} m from exposure {number})")
                continue
        matched.append(Exposure(path=image, mark=mark, exif=row))

    if unnumbered:
        raise P1GeoError(
            f"{len(unnumbered)} image(s) carry no four-digit exposure number, e.g. "
            f"{unnumbered[0]}; the exposure they belong to cannot be established"
        )
    if disagreeing:
        raise P1GeoError(
            f"{len(disagreeing)} image(s) sit further than {tolerance_m:g} m from the exposure "
            f"their filename points at, e.g. {disagreeing[0]}. The filename-to-exposure "
            "assumption does not hold for this flight; writing this file would georeference "
            "images to their neighbours' positions"
        )
    if not matched:
        raise P1GeoError(
            f"none of the {len(images)} image(s) matched an exposure in the mark file "
            f"(unmatched: {len(unmatched)})"
        )
    return matched


def _camera_position(mark: MarkRecord, *, apply_lever_arm: bool) -> tuple[float, float, float]:
    if not apply_lever_arm:
        return mark.longitude, mark.latitude, mark.ellipsoidal_height
    latitude = mark.latitude + mark.offset_north / _METRES_PER_DEGREE_LAT
    longitude = mark.longitude + mark.offset_east / (
        _METRES_PER_DEGREE_LON * math.cos(math.radians(mark.latitude))
    )
    return longitude, latitude, mark.ellipsoidal_height + mark.offset_up


def write_geo_file(
    exposures: list[Exposure],
    destination: Path,
    *,
    srs: str = DEFAULT_SRS,
    apply_lever_arm: bool = False,
    source: Path | None = None,
) -> GeoFile:
    """Write an ODM `geo.txt` for the images that are present. Positions only.

    **This costs the per-image RTK weighting.** ODM applies a geo entry after reading the XMP,
    and assigns the entry's accuracy unconditionally, so a file without accuracy columns clears
    the 1.3-2.3 cm standard deviations it had read from `@drone-dji:RtkStd*` and falls back on
    `--gps-accuracy` (ODM's default is 3 m). The accuracy columns cannot be filled honestly from
    here: they sit behind `yaw pitch roll`, ODM takes yaw from `FlightYawDegree` rather than the
    gimbal, and on the EXIF path it stores `90 + GimbalPitchDegree` for a DJI make while a geo
    entry is taken as given -- writing the raw gimbal angles would hand the bundle adjustment a
    90-degree pitch prior. So a caller who writes this file must set `--gps-accuracy` to the
    reported value, and is trading a per-image weight for a block-wide one.

    What the file does buy is precision. EXIF stores latitude and longitude as
    degrees-minutes-seconds rationals, which quantises a position to about 3 mm -- one to two
    pixels at the 1.8 mm ground sample distance a P1 reaches -- while the mark file carries the
    full solution. On its own that is not worth the trade above; with `apply_lever_arm` it is
    the only way to deliver the shifted positions at all.

    `apply_lever_arm` adds the mark file's antenna-to-camera offset to each position. It
    defaults to off because whether DJI has already applied it is unsettled: the EXIF position
    and the mark position are identical to three millimetres, so either both are the antenna or
    both are the camera, and nothing measurable here distinguishes them. The offset is not a
    uniform shift -- it holds a constant bearing relative to the flight line, so it reverses
    between opposite strips -- which is why the question matters and why it is left explicit
    rather than decided quietly. See `docs/decisions-and-verification.md` §2.16.
    """
    if not exposures:
        raise P1GeoError("no exposures to write")

    # The first line must be the SRS: ODM reads line 0 as the reference system. A comment there
    # would be taken as one.
    lines = [srs]
    lines.append(
        f"# {len(exposures)} images"
        + (f" from {source.name}" if source is not None else "")
        + "; positions from the mark file; lever arm "
        + ("applied" if apply_lever_arm else "not applied")
    )
    for exposure in exposures:
        longitude, latitude, height = _camera_position(
            exposure.mark, apply_lever_arm=apply_lever_arm
        )
        lines.append(f"{exposure.path.name} {longitude:.9f} {latitude:.9f} {height:.3f}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return GeoFile(
        path=destination,
        srs=srs,
        images=len(exposures),
        lever_arm_applied=apply_lever_arm,
    )


def describe(exposures: list[Exposure]) -> BlockGeolocation:
    """Summarise what the mark file says about a block, without writing anything."""
    if not exposures:
        raise P1GeoError("no exposures to describe")
    return BlockGeolocation(
        images=len(exposures),
        with_attitude=sum(1 for exposure in exposures if exposure.has_attitude),
        nadir_within_tolerance=sum(1 for exposure in exposures if exposure.is_nadir),
        horizontal_accuracy_m=_percentile(
            [exposure.mark.horizontal_accuracy for exposure in exposures], 0.95
        ),
        vertical_accuracy_m=_percentile([exposure.mark.std_up for exposure in exposures], 0.95),
        flags=tuple(sorted({exposure.mark.flag for exposure in exposures})),
        lever_arm=lever_arm(exposures),
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


@dataclass(frozen=True)
class LeverArm:
    """What the mark file says about the antenna-to-camera offset, for reporting."""

    median_horizontal_m: float
    max_horizontal_m: float
    median_up_m: float

    @property
    def worth_reporting(self) -> bool:
        # Below a centimetre it cannot matter at any GSD this pipeline produces.
        return self.max_horizontal_m > 0.01


def lever_arm(exposures: list[Exposure]) -> LeverArm:
    horizontal = [
        math.hypot(exposure.mark.offset_north, exposure.mark.offset_east) for exposure in exposures
    ]
    up = [exposure.mark.offset_up for exposure in exposures]
    return LeverArm(
        median_horizontal_m=statistics.median(horizontal) if horizontal else 0.0,
        max_horizontal_m=max(horizontal) if horizontal else 0.0,
        median_up_m=statistics.median(up) if up else 0.0,
    )
