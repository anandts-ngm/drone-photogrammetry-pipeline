"""Reading a DJI P1 flight's geolocation.

The lines below are copied from a real `Timestamp.MRK` and a real exiftool export, because the
spacing, the trailing commas and the label suffixes are the whole difficulty: a parser written
against a tidied-up version of this format works on nothing.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from drone_photogrammetry_pipeline.ingest.p1_geo import (
    P1GeoError,
    describe,
    exposure_number,
    match_exposures,
    read_mark_file,
    read_metadata_csv,
    write_geo_file,
)

MRK = """\
1	105977.009643	[2430]	   402,N	   591,E	   219,V	45.95639119,Lat	96.70787946,Lon	1798.906,Ellh	0.017806, 0.012926, 0.022497	52,Q
2	105979.370505	[2430]	   406,N	   565,E	   278,V	45.95639209,Lat	96.70791700,Lon	1798.129,Ellh	0.017818, 0.012997, 0.022922	52,Q
3	105980.354366	[2430]	  -413,N	  -580,E	  -232,V	45.95639312,Lat	96.70795799,Lon	1797.309,Ellh	0.017817, 0.013012, 0.022833	52,Q
"""

CSV = """\
SourceFile,FileName,FileSize,Model,GPSLatitude,GPSLongitude,AbsoluteAltitude,\
GimbalPitchDegree,GimbalYawDegree,GimbalRollDegree,RtkFlag,RtkStdLon,RtkStdLat,RtkStdHgt
./DJI_20260803132556_0001.JPG,DJI_20260803132556_0001.JPG,25139383,ZenmuseP1,\
45.9563911666667,96.7078794444444,+1798.906,-89.90,-90.60,+180.00,52,0.01293,0.01781,0.02250
./DJI_20260803132558_0002.JPG,DJI_20260803132558_0002.JPG,24336221,ZenmuseP1,\
45.9563920833333,96.7079169722222,+1798.129,-89.90,-90.60,+180.00,52,0.01300,0.01782,0.02292
"""


def write_flight(tmp_path: Path, *, images: int = 2, csv: str | None = CSV) -> Path:
    root = tmp_path / "DJI_202608031301_013_B084"
    root.mkdir()
    (root / "DJI_202608031301_013_B084_Timestamp.MRK").write_text(MRK, encoding="utf-8")
    names = [
        "DJI_20260803132556_0001.JPG",
        "DJI_20260803132558_0002.JPG",
        "DJI_20260803132559_0003.JPG",
    ]
    for name in names[:images]:
        (root / name).write_bytes(b"")
    if csv is not None:
        (root / "metadata.csv").write_text(csv, encoding="utf-8")
    return root


def mark_file(root: Path) -> Path:
    return root / "DJI_202608031301_013_B084_Timestamp.MRK"


def test_a_mark_line_is_read_field_by_field(tmp_path: Path) -> None:
    marks = read_mark_file(mark_file(write_flight(tmp_path)))

    assert sorted(marks) == [1, 2, 3]
    first = marks[1]
    assert first.latitude == pytest.approx(45.95639119)
    assert first.longitude == pytest.approx(96.70787946)
    assert first.ellipsoidal_height == pytest.approx(1798.906)
    # The three unlabelled numbers are north, east and up, in that order: the exiftool export
    # of the same exposure gives RtkStdLat 0.01781 and RtkStdLon 0.01293.
    assert first.std_north == pytest.approx(0.017806)
    assert first.std_east == pytest.approx(0.012926)
    assert first.std_up == pytest.approx(0.022497)
    # Offsets are millimetres in the file and metres everywhere else.
    assert first.offset_north == pytest.approx(0.402)
    assert first.offset_east == pytest.approx(0.591)
    assert first.offset_up == pytest.approx(0.219)
    assert first.flag == "52"


def test_a_negative_offset_keeps_its_sign(tmp_path: Path) -> None:
    """The offsets reverse between opposite flight strips, so the sign carries the meaning."""
    marks = read_mark_file(mark_file(write_flight(tmp_path)))

    assert marks[3].offset_north == pytest.approx(-0.413)
    assert marks[3].offset_east == pytest.approx(-0.580)


def test_horizontal_accuracy_combines_the_two_axes(tmp_path: Path) -> None:
    marks = read_mark_file(mark_file(write_flight(tmp_path)))

    assert marks[1].horizontal_accuracy == pytest.approx(math.hypot(0.017806, 0.012926))


def test_a_line_missing_a_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.MRK"
    path.write_text("1\t105977.0\t[2430]\t402,N\t591,E\t219,V\t1798.906,Ellh\t52,Q\n")

    with pytest.raises(P1GeoError, match="Lat"):
        read_mark_file(path)


def test_an_empty_mark_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.MRK"
    path.write_text("\n\n")

    with pytest.raises(P1GeoError, match="no exposure events"):
        read_mark_file(path)


def test_the_exposure_number_is_the_trailing_group_not_the_timestamp() -> None:
    """`DJI_20260803132556_0001.JPG` holds a 14-digit timestamp before the exposure number."""
    assert exposure_number(Path("DJI_20260803132556_0001.JPG")) == 1
    assert exposure_number(Path("DJI_20260803132715_0080.JPG")) == 80
    assert exposure_number(Path("orthophoto.tif")) is None


def test_only_the_images_present_are_matched(tmp_path: Path) -> None:
    """One mark file covers a whole flight; a folder holds one block of it."""
    root = write_flight(tmp_path, images=2)
    marks = read_mark_file(mark_file(root))

    matched = match_exposures(
        sorted(root.glob("*.JPG")), marks, read_metadata_csv(root / "metadata.csv")
    )

    assert len(marks) == 3
    assert [exposure.mark.exposure for exposure in matched] == [1, 2]


def test_exif_quantisation_does_not_count_as_a_disagreement(tmp_path: Path) -> None:
    """EXIF stores degrees-minutes-seconds rationals, so it differs by a few millimetres."""
    root = write_flight(tmp_path, images=2)

    matched = match_exposures(
        sorted(root.glob("*.JPG")),
        read_mark_file(mark_file(root)),
        read_metadata_csv(root / "metadata.csv"),
    )

    assert len(matched) == 2


def test_an_image_far_from_its_exposure_stops_the_run(tmp_path: Path) -> None:
    """A wrong filename-to-exposure rule would georeference every image to a neighbour."""
    moved = CSV.replace("45.9563911666667", "45.9600000000000")  # about 400 m north
    root = write_flight(tmp_path, images=2, csv=moved)

    with pytest.raises(P1GeoError, match="does not hold for this flight"):
        match_exposures(
            sorted(root.glob("*.JPG")),
            read_mark_file(mark_file(root)),
            read_metadata_csv(root / "metadata.csv"),
        )


def test_a_folder_whose_images_are_not_in_the_mark_file_is_refused(tmp_path: Path) -> None:
    root = write_flight(tmp_path, images=0)
    (root / "DJI_20260803140000_0900.JPG").write_bytes(b"")

    with pytest.raises(P1GeoError, match="matched an exposure"):
        match_exposures(sorted(root.glob("*.JPG")), read_mark_file(mark_file(root)))


def test_the_geo_file_carries_positions_only(tmp_path: Path) -> None:
    """Four columns, deliberately: ODM's accuracy columns sit behind an attitude that cannot be
    written correctly from a gimbal angle, and it already reads the RTK deviations from XMP."""
    root = write_flight(tmp_path, images=2)
    matched = match_exposures(
        sorted(root.glob("*.JPG")),
        read_mark_file(mark_file(root)),
        read_metadata_csv(root / "metadata.csv"),
    )

    written = write_geo_file(matched, tmp_path / "out" / "geo.txt", source=mark_file(root))

    lines = written.path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "EPSG:4326", "ODM reads the first line as the reference system"
    assert lines[1].startswith("#")
    rows = [line for line in lines[2:] if line]
    assert len(rows) == 2
    for row in rows:
        assert len(row.split()) == 4
    # x is longitude and y is latitude, in that order, which is the easiest thing here to
    # transpose and the hardest to notice.
    name, x, y, z = rows[0].split()
    assert name == "DJI_20260803132556_0001.JPG"
    assert float(x) == pytest.approx(96.70787946)
    assert float(y) == pytest.approx(45.95639119)
    assert float(z) == pytest.approx(1798.906)
    assert not written.lever_arm_applied


def test_the_lever_arm_shifts_every_position_when_asked(tmp_path: Path) -> None:
    root = write_flight(tmp_path, images=1)
    matched = match_exposures(
        sorted(root.glob("*.JPG")),
        read_mark_file(mark_file(root)),
        read_metadata_csv(root / "metadata.csv"),
    )

    plain = write_geo_file(matched, tmp_path / "plain.txt")
    shifted = write_geo_file(matched, tmp_path / "shifted.txt", apply_lever_arm=True)

    def position(path: Path) -> tuple[float, float, float]:
        row = next(line for line in path.read_text(encoding="utf-8").splitlines()[2:] if line)
        _, x, y, z = row.split()
        return float(x), float(y), float(z)

    east_before, north_before, up_before = position(plain.path)
    east_after, north_after, up_after = position(shifted.path)

    assert up_after - up_before == pytest.approx(0.219, abs=1e-3)
    # 0.402 m north and 0.591 m east, expressed in degrees at this latitude.
    assert (north_after - north_before) * 111_132.0 == pytest.approx(0.402, abs=0.01)
    assert (east_after - east_before) * 111_320.0 * math.cos(math.radians(45.956)) == pytest.approx(
        0.591, abs=0.01
    )
    assert shifted.lever_arm_applied


def test_the_description_reports_what_a_caller_has_to_decide_with(tmp_path: Path) -> None:
    root = write_flight(tmp_path, images=2)
    matched = match_exposures(
        sorted(root.glob("*.JPG")),
        read_mark_file(mark_file(root)),
        read_metadata_csv(root / "metadata.csv"),
    )

    found = describe(matched)

    assert found.images == 2
    assert found.flags == ("52",)
    assert found.nadir_within_tolerance == 2, "a gimbal at -89.9 degrees is pointing down"
    assert found.horizontal_accuracy_m == pytest.approx(0.022, abs=0.001)
    assert found.vertical_accuracy_m == pytest.approx(0.0229, abs=0.001)
    assert found.lever_arm.median_horizontal_m == pytest.approx(0.714, abs=0.01)


def test_without_an_exiftool_export_the_attitude_is_unknown_not_oblique(tmp_path: Path) -> None:
    root = write_flight(tmp_path, images=2, csv=None)
    matched = match_exposures(sorted(root.glob("*.JPG")), read_mark_file(mark_file(root)))

    found = describe(matched)

    assert found.images == 2
    assert not found.attitude_known
    assert found.nadir_within_tolerance == 0


def test_a_csv_without_gps_columns_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "metadata.csv"
    path.write_text("SourceFile,FileName\n./a.JPG,a.JPG\n", encoding="utf-8")

    with pytest.raises(P1GeoError, match="no rows"):
        read_metadata_csv(path)
