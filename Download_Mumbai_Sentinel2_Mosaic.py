"""
=============================================================================
Sentinel-2 Mosaic Downloader for Mumbai Flood AOI
  - Mosaics (stitches) ALL overlapping tiles across dates into ONE single GeoTIFF
  - Warps directly from AWS S3 to the target grid (extremely fast & memory-efficient)
  - Clips precisely to the Mumbai AOI polygon shape
  - 6 bands: B2=Blue, B3=Green, B4=Red, B8=NIR, B11=SWIR1, B12=SWIR2
  - CRS: EPSG:32643 (WGS 84 / UTM zone 43N)
  - Date: 2026-06-01 to 2026-06-05
  - Output: outputs/Mumbai_2026-06-01_to_2026-06-05_6BAND_EPSG32643_Mosaic.tif
=============================================================================
"""

import os, logging, warnings, datetime, numpy as np, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings('ignore')

# ── Fix PROJ / GDAL environment ───────────────────────────────────────────────
import pyproj
PROJ_DIR = pyproj.datadir.get_data_dir()
os.environ['PROJ_LIB']            = PROJ_DIR
os.environ['GDAL_DATA']           = PROJ_DIR
os.environ['PROJ_NETWORK']        = 'OFF'
os.environ['PROJ_DEBUG']          = '0'
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

import rasterio
from rasterio.env import Env
from rasterio.warp import reproject, Resampling
from rasterio.features import geometry_mask
import rasterio.transform
from shapely.geometry import shape
import geopandas as gpd
from pystac_client import Client

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
BASE_DIR   = Path(r"C:\Users\RohanDhanajiGhadage\OneDrive - AIQ Space Ventures Private Limited\BACKUP\AIQ\SAR Data")
SHP_PATH   = BASE_DIR / "Mumbai_Flood_AOI" / "Mumbai_Flood_AOI.shp"
OUTPUT_DIR = BASE_DIR / "outputs"
LOG_DIR    = BASE_DIR / "logs"

TARGET_EPSG = 32643  # WGS 84 / UTM zone 43N
PIXEL_SIZE  = 10.0   # 10 meters resolution

START_DATE = datetime.date(2026, 6, 1)
END_DATE   = datetime.date(2026, 6, 5)

# B2=Blue, B3=Green, B4=Red, B8=NIR, B11=SWIR1, B12=SWIR2
BAND_SELECTION = ['blue', 'green', 'red', 'nir', 'swir16', 'swir22']

for d in [OUTPUT_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
class NoEmojiFormatter(logging.Formatter):
    _e = re.compile("[" + u"\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
                    + u"\U0001F680-\U0001F6FF\U0001F700-\U0001FAFF"
                    + u"\U00002702-\U000027B0\U000024C2-\U0001F251" + "]+",
                    flags=re.UNICODE)
    def format(self, r): return self._e.sub('', super().format(r))

log_ts   = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
log_file = LOG_DIR / f"mumbai_s2_mosaic_{log_ts}.log"
logger   = logging.getLogger("MumbaiS2Mosaic")
logger.setLevel(logging.INFO)
fmt = '%(asctime)s | %(levelname)s | %(message)s'
for h in [logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()]:
    h.setFormatter(NoEmojiFormatter(fmt)); logger.addHandler(h)

# ══════════════════════════════════════════════════════════════════════════════
# HELPER TO WARP ONE BAND
# ══════════════════════════════════════════════════════════════════════════════

def warp_band_to_grid(item, band_name, dst_transform, dst_width, dst_height, dst_crs):
    """
    Directly warps a single band from the STAC asset URL into the destination grid.
    This requests only the necessary pixels from S3 on the fly.
    """
    # Prefer non-jp2 variants if available
    url = item.assets[band_name].href
    try:
        with rasterio.open(url) as src:
            band_dest = np.zeros((dst_height, dst_width), dtype='float32')
            reproject(
                source=rasterio.band(src, 1),
                destination=band_dest,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
                src_nodata=0,
                dst_nodata=0
            )
            return band_name, band_dest
    except Exception as e:
        # Retry with jp2 suffix if needed
        if not band_name.endswith('-jp2'):
            jp2_name = f"{band_name}-jp2"
            if jp2_name in item.assets:
                return warp_band_to_grid(item, jp2_name, dst_transform, dst_width, dst_height, dst_crs)
        logger.error(f"Failed to warp {band_name} for tile {item.id}: {e}")
        return band_name, None

# ══════════════════════════════════════════════════════════════════════════════
# MAIN MOSAIC PROCESS
# ══════════════════════════════════════════════════════════════════════════════

def main():
    logger.info("=" * 70)
    logger.info("STARTING SENTINEL-2 MOSAIC GENERATION")
    logger.info(f"Target CRS: EPSG:{TARGET_EPSG}")
    logger.info(f"Bands:      {BAND_SELECTION}")
    logger.info(f"Date range: {START_DATE} to {END_DATE}")
    logger.info("=" * 70)

    # ── Load AOI ────────────────────────────────────────────────────────────
    logger.info(f"Loading AOI: {SHP_PATH}")
    gdf_wgs84 = gpd.read_file(SHP_PATH).to_crs('epsg:4326')
    gdf_utm   = gdf_wgs84.to_crs(f'epsg:{TARGET_EPSG}')

    aoi_wgs84 = gdf_wgs84.union_all()
    aoi_utm   = gdf_utm.union_all()

    # ── Define Destination Grid ──────────────────────────────────────────────
    minx, miny, maxx, maxy = aoi_utm.bounds
    dst_width  = int(np.ceil((maxx - minx) / PIXEL_SIZE))
    dst_height = int(np.ceil((maxy - miny) / PIXEL_SIZE))
    dst_transform = rasterio.transform.from_origin(minx, maxy, PIXEL_SIZE, PIXEL_SIZE)
    dst_crs = pyproj.CRS.from_epsg(TARGET_EPSG).to_wkt()

    logger.info(f"Grid size : {dst_width} cols x {dst_height} rows")
    logger.info(f"Bounds    : {minx:.1f}, {miny:.1f}, {maxx:.1f}, {maxy:.1f}")

    # Initialize master canvases
    # canvas: bands x height x width
    canvas = np.zeros((len(BAND_SELECTION), dst_height, dst_width), dtype='float32')
    # Track which pixels have been filled to prioritize lower cloud cover
    filled_mask = np.zeros((dst_height, dst_width), dtype=bool)

    # ── STAC Search ─────────────────────────────────────────────────────────
    logger.info("Connecting to STAC...")
    catalog = Client.open("https://earth-search.aws.element84.com/v1")
    
    bbox = list(aoi_wgs84.bounds)
    time_filter = f"{START_DATE.isoformat()}T00:00:00Z/{END_DATE.isoformat()}T23:59:59Z"
    
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox, datetime=time_filter, max_items=100)
    
    items = list(search.get_all_items())
    logger.info(f"Found {len(items)} overlapping scenes total.")

    # Filter to only scenes that intersect our polygon
    intersecting_items = [it for it in items if shape(it.geometry).intersects(aoi_wgs84)]
    logger.info(f"Intersecting scenes: {len(intersecting_items)}")

    if not intersecting_items:
        logger.error("No scenes found overlapping the AOI polygon!"); return

    # Sort scenes by cloud cover DESCENDING (cloudiest first, clearest last)
    # This ensures that clear pixels overwrite cloudy ones on the canvas
    intersecting_items.sort(key=lambda x: x.properties.get('eo:cloud_cover', 100), reverse=True)

    # ── Warp and Mosaic ─────────────────────────────────────────────────────
    logger.info("\nMosaicing tiles onto canvas (cleanest pixels will end up on top)...")
    for idx, item in enumerate(intersecting_items, 1):
        tile_id = item.id.split('_')[1] if '_' in item.id else item.id
        date_str = item.id.split('_')[2][:8] if '_' in item.id else "unknown"
        cloud = item.properties.get('eo:cloud_cover', 100)
        logger.info(f"[{idx}/{len(intersecting_items)}] Processing {tile_id} ({date_str}) | Cloud cover: {cloud:.2f}%")

        # Warp all 6 bands in parallel for this tile
        tile_bands = {}
        with ThreadPoolExecutor(max_workers=min(6, len(BAND_SELECTION))) as executor:
            futures = {executor.submit(
                warp_band_to_grid, item, b, dst_transform, dst_width, dst_height, f"epsg:{TARGET_EPSG}"
            ): b for b in BAND_SELECTION}
            
            for fut in as_completed(futures):
                b_name, band_data = fut.result()
                # strip '-jp2' if present in key
                clean_name = b_name.replace('-jp2', '')
                if band_data is not None:
                    tile_bands[clean_name] = band_data

        # Blend this tile onto our master canvas
        if not tile_bands:
            continue

        # Create a mask where this tile has valid data (pixels > 0 in any band)
        # We use 'blue' band as the main reference for valid pixels
        ref_band_data = tile_bands.get('blue', next(iter(tile_bands.values())))
        valid_tile_pixels = (ref_band_data > 0)

        # Paint the pixels onto our canvas
        for band_idx, b_name in enumerate(BAND_SELECTION):
            if b_name in tile_bands:
                # Overwrite canvas pixels where the tile has valid data
                canvas[band_idx][valid_tile_pixels] = tile_bands[b_name][valid_tile_pixels]
        
        filled_mask[valid_tile_pixels] = True

    # ── Clip to Polygon Shape ────────────────────────────────────────────────
    logger.info("\nMasking out pixels outside the AOI polygon shape...")
    aoi_mask = geometry_mask(
        [aoi_utm],
        out_shape=(dst_height, dst_width),
        transform=dst_transform,
        invert=True  # True for pixels inside the polygon
    )

    # Set outside pixels to 0 (nodata)
    for idx in range(len(BAND_SELECTION)):
        canvas[idx][~aoi_mask] = 0

    # ── Export Final Mosaic GeoTIFF ──────────────────────────────────────────
    output_filename = OUTPUT_DIR / f"Mumbai_2026-06-01_to_2026-06-05_6BAND_EPSG{TARGET_EPSG}_Mosaic.tif"
    logger.info(f"Writing Mosaic to: {output_filename}")

    out_profile = {
        'driver': 'GTiff',
        'height': dst_height, 'width': dst_width,
        'count': len(BAND_SELECTION), 'dtype': 'float32',
        'crs': dst_crs, 'transform': dst_transform,
        'compress': 'lzw', 'tiled': True,
        'blockxsize': 256, 'blockysize': 256,
        'predictor': 2, 'bigtiff': 'IF_SAFER', 'nodata': 0,
    }

    with Env(PROJ_LIB=PROJ_DIR, GDAL_DATA=PROJ_DIR):
        with rasterio.open(output_filename, 'w', **out_profile) as dst:
            for idx in range(len(BAND_SELECTION)):
                dst.write(canvas[idx], idx + 1)
            for idx, name in enumerate(BAND_SELECTION, 1):
                dst.set_band_description(idx, name)

    size_mb = output_filename.stat().st_size / (1024 * 1024)
    logger.info(f"Saved Mosaic: {output_filename.name} ({size_mb:.1f} MB)")

    # ── Write Metadata ──────────────────────────────────────────────────────
    mf = OUTPUT_DIR / f"Mumbai_2026-06-01_to_2026-06-05_6BAND_EPSG{TARGET_EPSG}_Mosaic_metadata.txt"
    finite = canvas[canvas != 0]
    with open(mf, 'w') as f:
        f.write(f"Sentinel-2 Master Mosaic Metadata\n{'=' * 55}\n")
        f.write(f"Area         : Mumbai Flood AOI (Polygon Clipped Master Mosaic)\n")
        f.write(f"Date Range   : {START_DATE} to {END_DATE}\n")
        f.write(f"CRS          : EPSG:{TARGET_EPSG} (WGS84 / UTM Zone 43N)\n")
        f.write(f"Pixel Size   : {PIXEL_SIZE:.1f} m\n")
        f.write(f"Bands ({len(BAND_SELECTION):02d})  : {BAND_SELECTION}\n")
        f.write(f"Shape        : {canvas.shape}  (bands, rows, cols)\n")
        if finite.size > 0:
            f.write(f"Value Range  : {finite.min():.2f} - {finite.max():.2f}\n")
            f.write(f"Mean / Std   : {finite.mean():.2f} / {finite.std():.2f}\n")
        f.write(f"File Size    : {size_mb:.1f} MB\n")
        f.write(f"Output File  : {output_filename.name}\n")
    logger.info(f"Metadata: {mf.name}")
    logger.info("MOSAIC PROCESS COMPLETE!")

if __name__ == "__main__":
    main()
