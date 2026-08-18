# Master orthophoto raster standard

Status: draft for review.

Every approved master RGB orthophoto satisfies this contract, whether it came from P1 +
ODM, L2/L3 RGB + ODM, or an approved DJI Terra RGB/DOM export. The producing engine does
not influence the standard.

---

## 1. The contract

| Property | Required value |
|---|---|
| Format | GeoTIFF |
| Bands | 4 |
| Band interpretation | 1 red, 2 green, 3 blue, 4 alpha |
| Compression | `DEFLATE` |
| Lossy compression | forbidden anywhere in the file, including overviews |
| Tiled | yes |
| BigTIFF | yes |
| Resolution | native (see §6) |
| Resampling during packaging | none |
| Alpha channel | required, and it is the validity mask |
| NoData | no ambiguous `NoData=0` while alpha is the validity mask |
| Overviews | none in the delivered master |
| CRS | explicitly defined |
| Pixel size | recorded exactly in the manifest |
| Radiometric policy | documented, and identical across applicable blocks |

---

## 2. Why each clause exists

**Four bands with an explicit alpha.** Validity has to be unambiguous. A three-band file
plus `NoData=0` cannot distinguish "outside the survey" from "genuinely black ground",
which matters directly for the derived products this master is intended to feed
(iron-oxide indices, gossan indicators, RGB ratios, alteration proxies) — a false zero is
a false measurement.

**DEFLATE and no lossy compression.** The master is an analytical source. JPEG compression
would alter pixel values, and the alteration is spatially structured (block artefacts),
which is precisely the kind of signal that band-ratio products amplify.

**Tiled and BigTIFF.** Tiling makes windowed reads cheap for QA and for downstream
analysis. BigTIFF is required unconditionally rather than opportunistically so that the
delivered format does not silently change with file size — a 3.9 GB block and a 4.1 GB
block should not be different formats.

**No overviews.** Beyond file size, this is a correctness rule: ODM's overview builder uses
`gdaladdo -r average --config COMPRESS_OVERVIEW JPEG`, so an "overviews enabled" master
would contain lossy data inside a file that claims to be lossless. Overviews belong to the
derived visual product.

**Native resolution and no resampling.** Any resampling is a pixel-altering operation.
See §6.

---

## 3. GDAL realisation

Packaging is a straight copy with re-encoding — never a warp. The equivalent command form,
recorded in the manifest alongside the actual invocation:

```bash
gdal_translate SRC DST \
  -b 1 -b 2 -b 3 -b 4 \
  -colorinterp red,green,blue,alpha \
  -a_nodata none \
  -co COMPRESS=DEFLATE \
  -co PREDICTOR=2 \
  -co TILED=YES \
  -co BLOCKXSIZE=512 \
  -co BLOCKYSIZE=512 \
  -co BIGTIFF=YES \
  -co ALPHA=NON-PREMULTIPLIED \
  -co NUM_THREADS=ALL_CPUS
```

Notes:

- `PREDICTOR=2` is horizontal differencing. It is lossless and materially improves DEFLATE
  ratios on 8-bit imagery. ODM already uses it for DEFLATE/LZW.
- `BIGTIFF=YES` is deliberately stronger than ODM's `IF_SAFER`, which only produces BigTIFF
  when the file would otherwise overflow. `IF_SAFER` cannot satisfy an unconditional
  contract clause.
- `ALPHA=NON-PREMULTIPLIED` matters scientifically: premultiplied alpha scales RGB values by
  coverage at partially-transparent edge pixels, which would silently darken block margins
  and corrupt overlap comparisons. Confirm the accepted value against the pinned GDAL
  version before relying on it (tracked in `decisions-and-verification.md`).
- No `-tr`, `-ts`, `-outsize`, `-r`, and no `gdalwarp`. Absence of these is asserted, not
  assumed (§4).

---

## 4. Forbidden operations and asserted invariants

Packaging must not change the spatial raster grid. After packaging, the module asserts,
and the manifest records:

```text
width_out          == width_in
height_out         == height_in
geotransform_out   == geotransform_in      (exact equality on all six coefficients)
crs_out            == crs_in
band_count_out     == 4
```

If any assertion fails, packaging fails. It does not "fix" the output.

Four operations legitimately change the file and must therefore be recorded explicitly in
the manifest as applied operations, never performed silently:

1. **Alpha synthesis** from a NoData mask when the source has no alpha band (§5).
2. **Band selection** when the source has more than four bands.
3. **NoData removal** when the source carries a NoData value alongside a real alpha band.
4. **CRS declaration**, when a delivery documents a vertical reference that its file headers
   do not carry. This adds a vertical component only: the horizontal component is checked
   against the source and a mismatch is refused, because reinterpreting the horizontal CRS
   would relocate every pixel while leaving the geotransform untouched — a change that no
   grid comparison would catch. It is metadata-only and never resamples.

Any georeferencing workflow that genuinely requires a warp is a separate, explicitly
invoked stage — not part of packaging — and it records its resampling method, source and
target grids.

---

## 5. Alpha and NoData decision table

Applied to whatever the source hands over, ODM or Terra:

| Source condition | Action | Recorded as |
|---|---|---|
| 4 bands, band 4 already tagged alpha | copy; re-assert colour interpretation | `alpha: passthrough` |
| 4 bands, band 4 tagged alpha, **and** a NoData value also set | alpha wins; the NoData is dropped | `alpha: passthrough` + `nodata: dropped [...]` |
| 4 bands, band 4 present but untagged | retag colour interpretation only; no pixel change | `alpha: retagged` |
| 3 bands + internal GDAL mask band | derive alpha from the mask | `alpha: from_mask` |
| 3 bands + `NoData=0` only | **ambiguous** — requires explicit opt-in | `alpha: from_nodata` + QA `REVIEW` |
| 3 bands, no mask, no NoData | cannot establish validity | packaging fails |
| >4 bands | explicit band selection required in the profile | `bands: selected [...]` |

The second row is the common real case, not a corner case: every DOM in the Buduunkhad
delivery tags band 4 as alpha *and* sets `NoData=0` on all four bands. Note that GDAL
prefers a NoData value over the alpha band when it computes a dataset mask, so a source like
this reports nodata-derived mask flags despite having a perfectly good alpha band. The
packager resolves alpha from the colour interpretation first, precisely so that this does
not silently degrade into the ambiguous case below.

The `NoData=0` case is the one that must not be automated away. Deriving alpha from it
marks every legitimately black pixel as invalid. The pipeline will not do this by default;
it requires an explicit flag, and the resulting product is flagged `REVIEW` rather than
`PASS` so a human decides.

On the delivered master, the NoData value is **unset**. Alpha is the validity mask; keeping
both is the ambiguity the contract exists to remove.

---

## 6. Native resolution

Native means: preserve the raster grid that was actually produced. Do not round or
normalise it for convenience.

Real block resolutions such as 1.6 cm, 1.7 cm, 2.1 cm, 2.5 cm, 3.4 cm and 5.0 cm are all
acceptable when they represent actual photogrammetric output. The pipeline never converts
1.74 cm → 2.0 cm or 2.46 cm → 2.5 cm to make neighbouring files look uniform, and no
resolution range is hard-coded as required.

`pixel_size_x` and `pixel_size_y` are read back from the produced raster and recorded in
the manifest exactly, without rounding.

A related trap on the ODM path: `--orthophoto-resolution` is documented as "capped by a
ground sampling distance (GSD) estimate", so the requested value is a target, not a
guarantee. The profile records what was requested; the manifest records what was produced;
the two are compared and the difference is reported as information, never corrected by
resampling.

---

## 7. Raster QA checks

Each check states exactly which contract clause it verifies and how it is measured.
Measurement is via Rasterio/GDAL on the delivered file — the QA reads the product, it does
not trust the process that made it.

| Check | Method | Fail condition |
|---|---|---|
| `readable` | open the dataset | open raises |
| `is_geotiff` | driver name | `!= "GTiff"` |
| `band_count` | `ds.count` | `!= 4` |
| `colorinterp_red` / `_green` / `_blue` / `_alpha` | `ds.colorinterp` | band 1–4 not red/green/blue/alpha |
| `compression` | `IMAGE_STRUCTURE` metadata | `!= "DEFLATE"` |
| `no_lossy_compression` | compression of main image and every overview | any JPEG/LZMA-lossy variant |
| `tiled` | `ds.profile["tiled"]` / block shape | not tiled |
| `bigtiff` | TIFF header version field: `42` = classic, `43` = BigTIFF | `!= 43` |
| `crs_present` | `ds.crs` | `None` or undefined |
| `pixel_size_present` | `ds.transform` | missing, zero, or rotated (`b != 0` or `d != 0`) |
| `alpha_present` | band 4 colour interpretation | not alpha |
| `nodata_policy` | `ds.nodatavals` | any band declares NoData while alpha is the mask |
| `overview_count` | `ds.overviews(1)` | non-empty |

`bigtiff` is checked by reading the file header directly rather than inferring it from
size, because it is a format property and the whole point of the clause is that it does not
depend on size.

Advisory (recorded, not gating in milestone 1): TIFF `ExtraSamples` value, to confirm alpha
is unassociated rather than premultiplied.

---

## 8. QA result

Machine-readable, written to `qa/raster_qa.json`:

```json
{
  "status": "PASS",
  "checks": {
    "rgba": true,
    "compression": "DEFLATE",
    "tiled": true,
    "alpha": true,
    "overview_count": 0
  }
}
```

The delivered schema is richer than this illustration: every check carries its own
pass/fail, the observed value, and the contract clause it maps to. A failure states exactly
which contract was violated and what was observed instead — `"expected COMPRESS=DEFLATE,
observed JPEG"`, not `"raster QA failed"`.

`status` is one of `PASS`, `REVIEW`, `FAIL`. `REVIEW` exists for products that meet the
letter of the contract through a route that needs human sign-off, such as alpha derived
from an ambiguous NoData.

---

## 9. MASTER versus VISUAL

The master is an analytical source. It prioritises radiometric consistency, geometry,
traceability and lossless pixels.

A separate derived product may later prioritise appearance:

| | `ORTHO_MASTER` | `ORTHO_VISUAL` |
|---|---|---|
| Colour balancing | forbidden | allowed |
| Contrast enhancement | forbidden | allowed |
| JPEG compression | forbidden | allowed |
| Overviews | forbidden | allowed |
| Resampling | forbidden | allowed |

The visual product is always derived *from* the master and never replaces it. No visual
requirement may be satisfied by modifying the master.
