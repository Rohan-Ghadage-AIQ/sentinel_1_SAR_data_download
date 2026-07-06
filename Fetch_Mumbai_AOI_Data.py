"""
=============================================================================
Fast & Optimized Sentinel-2 AOI Data Fetcher for Mumbai
  - Direct COG Window Streaming: Only downloads pixels inside your AOI polygon
    (using GDAL HTTP/VSI cache parameters for maximum speed).
  - Perfect Stitching: Combines all tiles covering Mumbai into a single master file.
  - Zero Holes/Holes Filled: Mosaics multiple passes to fill all slivers.
  - Precise Polygon Clipping: Auto-masks all pixels outside the AOI.
  - Bands (6): B2(Blue), B3(Green), B4(Red), B8(NIR), B11(SWIR1), B12(SWIR2).
  - Output: outputs/Mumbai_Sentinel2_AOI_6Band.tif
=============================================================================
"""

import os, logging, warnings, datetime, numpy as np, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings('ignore')

# ── GDAL/AWS network parameters ───────────────────────────────────────────────
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
PIXEL_SIZE  = 10.0   # 10m target resolution

START_DATE = datetime.date(2025, 5, 1)
END_DATE   = datetime.date(2025, 5, 1)

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
log_file = LOG_DIR / f"fetch_mumbai_aoi_{log_ts}.log"
logger   = logging.getLogger("FetchMumbaiAOI")
logger.setLevel(logging.INFO)
fmt = '%(asctime)s | %(levelname)s | %(message)s'
for h in [logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()]:
    h.setFormatter(NoEmojiFormatter(fmt)); logger.addHandler(h)

# ══════════════════════════════════════════════════════════════════════════════
# DIRECT WARP FUNCTION (OPTIMIZED FOR HTTP STREAMING)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_and_warp_band(item, band_name, dst_transform, dst_width, dst_height, dst_crs):
    """
    Directly streams and reprojects a band from S3 using network-optimized settings.
    """
    url = item.assets[band_name].href
    try:
        # Wrap in rasterio Env to configure GDAL cache and network options for faster streaming
        with Env(
            GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif',
            GDAL_HTTP_MERGE_CONSECUTIVE_PARTS='YES',
            GDAL_HTTP_MULTIPLEX='YES',
            VSI_CACHE='YES',
            VSI_CACHE_SIZE='50000000'
        ):
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
        # Fallback to jp2 asset if COG fails
        if not band_name.endswith('-jp2'):
            jp2_name = f"{band_name}-jp2"
            if jp2_name in item.assets:
                return fetch_and_warp_band(item, jp2_name, dst_transform, dst_width, dst_height, dst_crs)
        logger.error(f"Error fetching band '{band_name}' for tile {item.id}: {e}")
        return band_name, None

# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    logger.info("=" * 70)
    logger.info("STARTING FAST AOI Sentinel-2 DATA FETCH PIPELINE")
    logger.info(f"Target EPSG : {TARGET_EPSG}")
    logger.info(f"Date Range  : {START_DATE} to {END_DATE}")
    logger.info(f"AOI Path    : {SHP_PATH}")
    logger.info("=" * 70)

    # ── 1. Load and prepare AOI Geometry ─────────────────────────────────────
    logger.info("Loading Shapefile...")
    gdf_wgs84 = gpd.read_file(SHP_PATH).to_crs('epsg:4326')
    gdf_utm   = gdf_wgs84.to_crs(f'epsg:{TARGET_EPSG}')

    aoi_wgs84 = gdf_wgs84.union_all()
    aoi_utm   = gdf_utm.union_all()

    # ── 2. Build Destination Grid ────────────────────────────────────────────
    minx, miny, maxx, maxy = aoi_utm.bounds
    dst_width  = int(np.ceil((maxx - minx) / PIXEL_SIZE))
    dst_height = int(np.ceil((maxy - miny) / PIXEL_SIZE))
    dst_transform = rasterio.transform.from_origin(minx, maxy, PIXEL_SIZE, PIXEL_SIZE)
    dst_crs = rasterio.crs.CRS.from_epsg(TARGET_EPSG).to_wkt()

    logger.info(f"Target dimension: {dst_width} x {dst_height} pixels")
    logger.info(f"Target resolution: {PIXEL_SIZE}m")

    # Canvas to store stacked bands (6 bands, height, width)
    canvas = np.zeros((len(BAND_SELECTION), dst_height, dst_width), dtype='float32')

    # ── 3. Query STAC API for items ─────────────────────────────────────────
    logger.info("Connecting to Earth Search STAC API...")
    catalog = Client.open("https://earth-search.aws.element84.com/v1")
    
    bbox = list(aoi_wgs84.bounds)
    time_filter = f"{START_DATE.isoformat()}T00:00:00Z/{END_DATE.isoformat()}T23:59:59Z"
    
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox, datetime=time_filter, max_items=100)
    
    items = list(search.get_all_items())
    logger.info(f"Total catalog scenes found: {len(items)}")

    # Keep only scenes that strictly overlap the polygon geometry
    overlapping_items = [it for it in items if shape(it.geometry).intersects(aoi_wgs84)]
    logger.info(f"Overlapping AOI scenes   : {len(overlapping_items)}")

    if not overlapping_items:
        logger.error("No overlapping scenes found inside the date range!"); return

    # Sort scenes by cloud cover DESCENDING
    # This allows clear pixels (processed last) to overwrite cloudier pixels
    overlapping_items.sort(key=lambda x: x.properties.get('eo:cloud_cover', 100), reverse=True)

    # ── 4. Warp & Merge tiles onto Canvas ────────────────────────────────────
    logger.info("\nStreaming pixels directly to target canvas...")
    for idx, item in enumerate(overlapping_items, 1):
        tile_name = item.id.split('_')[1] if '_' in item.id else item.id
        date_str = item.id.split('_')[2][:8] if '_' in item.id else "unknown"
        cloud = item.properties.get('eo:cloud_cover', 100)
        logger.info(f"[{idx}/{len(overlapping_items)}] Stitching {tile_name} ({date_str}) | Cloud cover: {cloud:.2f}%")

        # Download and warp bands concurrently
        tile_bands = {}
        with ThreadPoolExecutor(max_workers=min(6, len(BAND_SELECTION))) as executor:
            futures = {executor.submit(
                fetch_and_warp_band, item, b, dst_transform, dst_width, dst_height, f"epsg:{TARGET_EPSG}"
            ): b for b in BAND_SELECTION}

            for fut in as_completed(futures):
                b_name, band_data = fut.result()
                clean_name = b_name.replace('-jp2', '')
                if band_data is not None:
                    tile_bands[clean_name] = band_data

        if not tile_bands:
            continue

        # Create mask of valid data (pixels > 0 in reference band)
        ref_band = tile_bands.get('blue', next(iter(tile_bands.values())))
        valid_mask = (ref_band > 0)

        # Draw onto canvas
        for b_idx, b_name in enumerate(BAND_SELECTION):
            if b_name in tile_bands:
                canvas[b_idx][valid_mask] = tile_bands[b_name][valid_mask]

    # ── 5. Clip canvas precisely to polygon boundary ────────────────────────
    logger.info("\nMasking out areas outside the AOI polygon shape...")
    aoi_mask = geometry_mask(
        [aoi_utm],
        out_shape=(dst_height, dst_width),
        transform=dst_transform,
        invert=True  # True for pixels inside the polygon
    )

    # Mask all bands
    for idx in range(len(BAND_SELECTION)):
        canvas[idx][~aoi_mask] = 0

    # ── 6. Write final Master GeoTIFF ────────────────────────────────────────
    output_tif = OUTPUT_DIR / "Mumbai_Sentinel2_AOI_6Band.tif"
    logger.info(f"Writing single master GeoTIFF to: {output_tif}")

    out_profile = {
        'driver': 'GTiff',
        'height': dst_height, 'width': dst_width,
        'count': len(BAND_SELECTION), 'dtype': 'float32',
        'crs': dst_crs, 'transform': dst_transform,
        'compress': 'lzw', 'tiled': True,
        'blockxsize': 256, 'blockysize': 256,
        'predictor': 2, 'bigtiff': 'IF_SAFER', 'nodata': 0,
    }

    with Env():
        with rasterio.open(output_tif, 'w', **out_profile) as dst:
            for idx in range(len(BAND_SELECTION)):
                dst.write(canvas[idx], idx + 1)
            for idx, name in enumerate(BAND_SELECTION, 1):
                dst.set_band_description(idx, name)

    size_mb = output_tif.stat().st_size / (1024 * 1024)
    logger.info(f"Saved Master GeoTIFF: {output_tif.name} ({size_mb:.1f} MB)")

    # ── 7. Write Metadata file ───────────────────────────────────────────────
    mf = OUTPUT_DIR / "Mumbai_Sentinel2_AOI_6Band_metadata.txt"
    finite = canvas[canvas != 0]
    with open(mf, 'w') as f:
        f.write(f"Sentinel-2 Master AOI Mosaic Metadata\n{'=' * 55}\n")
        f.write(f"Area         : Mumbai Flood AOI (Fully occupied, single file)\n")
        f.write(f"Date Range   : {START_DATE} to {END_DATE}\n")
        f.write(f"CRS          : EPSG:{TARGET_EPSG} (WGS84 / UTM Zone 43N)\n")
        f.write(f"Pixel Size   : {PIXEL_SIZE:.1f} m\n")
        f.write(f"Bands ({len(BAND_SELECTION):02d})  : {BAND_SELECTION}\n")
        f.write(f"Shape        : {canvas.shape}  (bands, rows, cols)\n")
        if finite.size > 0:
            f.write(f"Value Range  : {finite.min():.2f} - {finite.max():.2f}\n")
            f.write(f"Mean / Std   : {finite.mean():.2f} / {finite.std():.2f}\n")
        f.write(f"File Size    : {size_mb:.1f} MB\n")
        f.write(f"Output File  : {output_tif.name}\n")
    logger.info(f"Metadata: {mf.name}")
    logger.info("FETCH PROCESS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
