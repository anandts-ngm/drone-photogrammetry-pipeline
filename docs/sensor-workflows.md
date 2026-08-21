# Sensor workflows

Status: draft for review.

Two producing paths, one product. This document describes what each path does, what is
known about it, and what still has to be established by benchmark rather than assumed.

---

## 1. Sensors are configuration, not code branches

There is no `if sensor == "P1"` anywhere in the pipeline. A sensor is an identifier that
selects a versioned profile document:

```text
profiles/
├── p1_35.yaml
├── p1_50.yaml
├── l2_rgb.yaml
└── l3_rgb.yaml
```

Initial identifiers: `P1_35`, `P1_50`, `L2_RGB`, `L3_RGB`. Adding a sensor means adding a
profile and a registry entry, not editing processing logic. ODM option names and values
appear only inside profiles.

P1 and L-camera imagery are **not** assumed to want the same ODM settings. They share
infrastructure and the master contract; they do not share tuning.

---

## 2. Path A — DJI P1

The primary production path, intended to replace the manual Metashape workflow.

```text
P1 JPG + RTK/PPK/MRK + optional GCP
        │
        ▼  ingest + validation        ── typed Block, severity-classified findings
        │
        ▼  navigation resolve         ── CRS, height type, accuracies, source
        │
        ▼  ODM via NodeODM            ── profile p1_35 / p1_50
        │
        ▼  engine outputs             ── orthophoto, DSM, point cloud
        │
        ▼  master packaging           ── GDAL, no resampling
        │
        ▼  QA                         ── raster contract now; CP / LiDAR later
        │
        ▼  P1 MASTER                  ── on explicit promotion only
```

The objective is to reproduce the **required product behaviour and QA result** of the
current workflow — photo ingestion, alignment, bundle adjustment, dense reconstruction,
DSM, orthophoto, point cloud, georeferencing, reporting — not to replicate individual
Metashape UI settings one-for-one.

### P1 specifics to establish

- **Camera recognition.** ODM must identify the P1 body/lens combination from EXIF and
  find a matching entry in its sensor database. Community reports indicate the P1 needed a
  sensor profile added and had issues with yaw/pitch/roll read from XMP. This must be
  confirmed empirically against the pinned ODM version with a real block; if the camera is
  unknown, the fallback is `--cameras` with a calibration produced from a reference block.
- **35 mm vs 50 mm.** Separate profiles from the start. Different GSD at the same altitude,
  different overlap behaviour, different feature-matching characteristics. Do not share.
- **MRK / PPK.** The mark file is now read — see below — but no PPK solution is computed here.

### What a P1 folder actually contains, and what to do with it

Measured on `E:\Buduunkhad P1 raw\DJI_202608031301_013_B084`:

```text
DJI_202608031301_013_B084/
  DJI_20260803132556_0001.JPG ... _0080.JPG    79 images (0030 is absent)
  DJI_202608031301_013_B084_Timestamp.MRK      649 exposures — the whole flight
  DJI_202608031301_013_B084_PPKOBS.obs         raw observables
  DJI_202608031301_013_B084_PPKNAV.nav
  DJI_202608031301_013_B084_PPKRAW.bin
  metadata.csv                                 exiftool export, 649 rows
  exiftool.exe
```

Two things follow. The mark file and the exiftool export both cover the **flight**, while the
folder holds one **block** of it, so anything read from them has to be filtered to the images
actually present. And the positions are already RTK-fixed: every exposure reports flag 52 with
standard deviations of 1.3–2.3 cm, which makes a PPK re-solve largely redundant for this
delivery.

```bash
drone-photo p1-geo "<flight folder>" --project-id "Buduunkhad P1"   # check before submitting
drone-photo validate "<flight folder>"
drone-photo process "<flight folder>" --profile p1_35
```

`p1-geo` reports rather than writes, because ODM already reads each image's RTK standard
deviations from its XMP and a positions-only `geo.txt` would replace them with ODM's 3 m
default. It exists to catch, before hours of reconstruction, the things that are invisible
afterwards: an image whose EXIF position does not match the exposure its filename points at, a
non-uniform RTK flag, or a gimbal that was not pointing down. See
`docs/decisions-and-verification.md` §2.16, including the unresolved antenna-to-camera lever
arm and `--apply-lever-arm`, which is the experiment that would settle it.

---

## 3. Path B — L2 / L3 RGB

The L2/L3 acquisition carries both LiDAR and an RGB mapping camera. **Only the RGB
orthophoto component is in scope here.** The LiDAR path is untouched and remains with
DJI Terra.

It is not yet established that every L2/L3 acquisition can be reconstructed in ODM — the
RGB camera is a mapping camera flown to LiDAR mission parameters, not to photogrammetric
ones, so overlap, exposure and geometry may be unsuitable. The architecture therefore
carries two routes.

### Route B1 — raw L-camera RGB through ODM (preferred target)

```text
L2/L3 RGB JPG + geolocation → validation → ODM/NodeODM → RGB ortho → packaging → QA → RGB MASTER
```

### Route B2 — approved DJI Terra RGB / DOM (fallback and migration source)

```text
DJI Terra RGB / DOM → external ingest → packaging → QA → RGB MASTER
```

Route B2 performs **no reconstruction**. It accepts an already-produced orthophoto,
records its provenance as external, and puts it through the identical packaging and QA.

**DJI Terra and Metashape are not available locally.** Their outputs are inputs to this
repository and can never be re-run or re-exported from here. That makes Route B2 the only
way these products enter the pipeline, and it rules out "re-export it from Terra" as a
remedy for any defect found in QA — whatever is wrong with a delivered file must either be
fixable losslessly during packaging or be escalated to the supplier.

What a real Terra DOM looks like, measured across all 79 zones of the Buduunkhad delivery
(details in `decisions-and-verification.md` §3.1.2): GeoTIFF, 4 bands uint8, band 4 tagged
alpha, DEFLATE, tiled 256×256, BigTIFF, no overviews, `EPSG:32647` horizontal only — and
`NoData=0` set on all four bands, which is the single clause of the master contract they
violate. Packaging keeps the alpha band, drops the NoData, and optionally declares the
documented vertical reference.

### Convergence

```text
P1 ODM ortho ─────────┐
L2 ODM ortho ─────────┤
L3 ODM ortho ─────────┼──► SourceOrtho ──► MASTER PACKAGER ──► QA ──► MASTER
Terra RGB / DOM ──────┘
```

The two routes converge at `SourceOrtho`, before packaging. From that point the product is
identical in contract regardless of origin, and the manifest records which route produced
it (`source_type`, `processing_engine`). This lets L-camera orthophoto generation migrate
to ODM block by block without any change to the delivered standard.

### L-camera specifics to establish

- Whether the RGB imagery from a LiDAR-parameterised flight has sufficient forward/side
  overlap for ODM reconstruction at acceptable quality.
- Whether the L2 RGB camera (4/3 CMOS) is present in ODM's sensor database.
- **L3 sensor characteristics are not yet supplied.** The profile `l3_rgb.yaml` will be
  created as a placeholder with explicit `TODO` markers rather than guessed values.
- What Terra DOM exports actually look like — band count, alpha vs NoData, compression,
  embedded overviews, CRS declaration. This determines how much work Route B2's packager
  must do. See `raster-standard.md` §5 for the alpha/NoData decision table that handles the
  possibilities; one real Terra DOM is needed to confirm which branch applies.

---

## 4. Processing unit

One acquisition block = one processing task. Blocks such as `B044`, `B051`, `B064`,
`B066`, `N003`, `N007`, `PR01` remain independently traceable from raw input to final
output.

Unrelated survey blocks are never merged into a single ODM reconstruction for convenience.
Doing so would destroy per-block traceability and make the radiometric overlap comparisons
in `radiometry.md` impossible, since those comparisons depend on blocks having been
processed independently.

---

## 5. Navigation sources

Embedded EXIF is **not** assumed to be the authoritative navigation solution. Navigation is
an abstraction with several eventual implementations:

```text
NavigationSource
├── EXIF RTK            ← milestone 1, read-only
├── MRK                 ← interface only
├── local PPK           ← interface only
├── base-station solution   ← interface only
└── external corrected trajectory ← interface only
```

A vertical reference may exist only in an accompanying document. The Buduunkhad delivery is
the case in point: none of its rasters or point clouds carry a vertical CRS in their
headers, the heights are Baltic 1977 **normal** heights, and the geoid was already applied
in the field — reapplying it would introduce roughly 48 m of error. Nothing in the files
reveals any of that. This is why the vertical reference is declared in `block.yaml` and
never inferred, and why `HeightType` distinguishes `NORMAL` from `ORTHOMETRIC`.

Whichever source is used, the resolved `NavigationSolution` must record:

```text
horizontal CRS
vertical datum / height type   (ellipsoidal | orthometric | local mine datum)
navigation source
XY accuracy
Z accuracy
base station
processing method
```

Height types are never silently mixed. If the vertical reference cannot be determined, the
run either fails or is explicitly marked unsuitable for absolute-Z QA — it is never
guessed, and it never quietly defaults to ellipsoidal.

---

## 6. GCP and Check Points are different things

| | GCP | Check Point |
|---|---|---|
| Influences the adjustment | yes | **no** |
| Passed to ODM | yes, via `--gcp` | **never** |
| Purpose | georeferencing constraint | independent QA |
| Stored in | `control/` | `checkpoints/` |

These datasets are never automatically merged, and a Check Point is never converted into a
GCP merely because ODM accepts GCP input. They are separate types in the model layer so
that passing a Check Point where a GCP is expected is a type error.

Note on ODM behaviour: `--use-exif` means "I have a GCP file but use EXIF for
georeferencing instead". It is a georeferencing-source switch, not a way to feed check
points, and it does not turn GCPs into check points.

Internal bundle-adjustment residuals are **not** a substitute for Check Point QA. An
adjustment can report excellent internal consistency while being systematically shifted.

---

## 7. Input structure

```text
BLOCK_ID/
├── imagery/          *.JPG
├── navigation/       MRK, RTK, PPK, geo source files
├── reference/        flight log, base station metadata, CRS metadata,
│                     height datum metadata, LiDAR reference
├── control/          GCP
├── checkpoints/      independent CP
└── block.yaml
```

Not all directories are mandatory. Validation classifies every expectation into one of four
outcomes, and the distinction between the last two is the point of the exercise:

| Severity | Meaning | Effect |
|---|---|---|
| `REQUIRED_PRESENT` | required and found | none |
| `OPTIONAL_MISSING` | optional, absent | recorded, run proceeds |
| `MISSING_ACCEPTABLE` | expected but justified absent (e.g. no GCP on an RTK-only block) | recorded, run proceeds, may restrict QA scope |
| `MISSING_FATAL` | absent and the run cannot be trusted (e.g. no vertical datum declaration on a block requiring absolute-Z QA) | run stops |

A missing Check Point set is not fatal — it downgrades the QA that can be claimed, and the
manifest records that the product was never independently checked.

---

## 8. What this repository does not do

LiDAR trajectory/POS processing, point-cloud reconstruction, ground classification, LiDAR
DTM generation and LiDAR strip QA remain in DJI Terra. This repository consumes approved
LiDAR products as optional external geometry references and never produces or modifies
them. LiDAR is preferred when available but is never mandatory for a run.
