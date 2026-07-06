"""
Cross-verify all downloaded Sentinel-2 GeoTIFFs
Run:  python verify_downloads.py
"""
import os, sys
os.environ['PROJ_NETWORK'] = 'OFF'
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

import pyproj
os.environ['PROJ_LIB']  = pyproj.datadir.get_data_dir()
os.environ['GDAL_DATA'] = pyproj.datadir.get_data_dir()

from pathlib import Path
import rasterio
import numpy as np

OUTPUT_DIR = Path(r"C:\Users\RohanDhanajiGhadage\OneDrive - AIQ Space Ventures Private Limited\BACKUP\AIQ\SAR Data\Mumbai_Sentinel2_Outputs")
EXPECTED_EPSG = 32643
EXPECTED_BANDS = 16  # 15 data bands + 1 alpha

tifs = sorted(OUTPUT_DIR.glob("*.tif"))
if not tifs:
    print("No .tif files found!"); sys.exit(1)

print(f"\n{'='*90}")
print(f"  SENTINEL-2 DOWNLOAD VERIFICATION  |  {len(tifs)} files found")
print(f"{'='*90}\n")

all_ok = True
results = []

for tif in tifs:
    print(f"Checking: {tif.name}")
    issues = []

    try:
        with rasterio.open(tif) as src:
            # 1. CRS check
            native_epsg = None
            try:
                native_epsg = pyproj.CRS.from_wkt(src.crs.to_wkt()).to_epsg()
            except:
                native_epsg = src.crs.to_epsg()

            if native_epsg == EXPECTED_EPSG:
                crs_status = f"EPSG:{native_epsg} OK"
            else:
                crs_status = f"EPSG:{native_epsg} WRONG!"
                issues.append(f"CRS mismatch: expected {EXPECTED_EPSG}, got {native_epsg}")

            # 2. Band count
            band_count = src.count
            if band_count == EXPECTED_BANDS:
                band_status = f"{band_count} OK"
            else:
                band_status = f"{band_count} (expected {EXPECTED_BANDS})"
                if band_count < 2:
                    issues.append(f"Only {band_count} band(s)")

            # 3. Band names
            band_names = [src.descriptions[i] if src.descriptions[i] else f"band_{i+1}"
                          for i in range(band_count)]

            # 4. Shape & resolution
            height, width = src.height, src.width
            pixel_x = abs(src.transform.a)
            pixel_y = abs(src.transform.e)

            # 5. Read a sample band and check values
            sample = src.read(1)  # first band
            valid_pixels = sample[sample != 0]
            if valid_pixels.size > 0:
                vmin, vmax = valid_pixels.min(), valid_pixels.max()
                vmean = valid_pixels.mean()
                # Sentinel-2 L2A reflectance values typically 0-10000
                if vmax > 65000:
                    issues.append(f"Suspicious max value: {vmax}")
                value_status = f"{vmin:.0f}-{vmax:.0f} (mean={vmean:.0f})"
            else:
                value_status = "ALL ZEROS!"
                issues.append("All pixels are zero/nodata")

            # 6. Check for excessive nodata
            total_pixels = sample.size
            nodata_pct = ((sample == 0).sum() / total_pixels) * 100
            if nodata_pct > 90:
                issues.append(f"{nodata_pct:.1f}% nodata")

            # 7. File size
            size_mb = tif.stat().st_size / (1024*1024)

            status = "PASS" if not issues else "WARN"
            if any("WRONG" in i or "ALL ZEROS" in i for i in issues):
                status = "FAIL"
                all_ok = False

            results.append({
                'file': tif.name,
                'status': status,
                'crs': crs_status,
                'bands': band_status,
                'shape': f"{height}x{width}",
                'pixel': f"{pixel_x:.1f}m",
                'values': value_status,
                'nodata%': f"{nodata_pct:.1f}%",
                'size_mb': f"{size_mb:.1f}",
                'band_names': band_names,
                'issues': issues,
            })

    except Exception as e:
        results.append({
            'file': tif.name,
            'status': 'ERROR',
            'issues': [str(e)],
        })
        all_ok = False

# ── Print Summary Table ──────────────────────────────────────────────────
print(f"\n{'='*120}")
print(f"{'File':<55} {'Status':>6} {'CRS':>14} {'Bands':>7} {'Shape':>12} {'Pixel':>6} {'NoData':>7} {'Size':>8}")
print(f"{'-'*120}")
for r in results:
    if r['status'] == 'ERROR':
        print(f"{r['file']:<55} {'ERROR':>6}   {r['issues'][0]}")
        continue
    print(f"{r['file']:<55} {r['status']:>6} {r['crs']:>14} {r['bands']:>7} {r['shape']:>12} {r['pixel']:>6} {r['nodata%']:>7} {r['size_mb']:>6} MB")
    if r['issues']:
        for iss in r['issues']:
            print(f"  >> {iss}")

print(f"{'-'*120}")

# ── Band Names ───────────────────────────────────────────────────────────
print(f"\nBand names (from first file):")
if results and 'band_names' in results[0]:
    for i, name in enumerate(results[0]['band_names'], 1):
        print(f"  Band {i:2d}: {name}")

# ── Final Verdict ────────────────────────────────────────────────────────
print(f"\n{'='*90}")
if all_ok:
    print("  ALL FILES PASSED VERIFICATION!")
else:
    print("  SOME FILES HAVE ISSUES - check warnings above")
print(f"{'='*90}\n")

# ── Also check metadata files ────────────────────────────────────────────
meta_files = sorted(OUTPUT_DIR.glob("*_metadata.txt"))
print(f"Metadata files: {len(meta_files)} / {len(tifs)} expected")
if len(meta_files) != len(tifs):
    print("  WARNING: Missing metadata files!")
    tif_basenames = {t.stem.replace('.tif','') for t in tifs}
    meta_basenames = {m.stem.replace('_metadata','') for m in meta_files}
    missing = tif_basenames - meta_basenames
    if missing:
        print(f"  Missing metadata for: {missing}")

print(f"\nTotal disk usage: {sum(t.stat().st_size for t in tifs) / (1024*1024*1024):.2f} GB")
print("Done.")
