"""
=============================================================================
Universal Sentinel-1 (SAR) & Sentinel-2 (Optical) AOI Data Fetcher
  - General-purpose: Works with ANY input shapefile anywhere in India/globally.
  - Auto-Projection: Automatically determines the correct UTM EPSG projection
    based on the shapefile's centroid.
  - Direct COG Window Streaming: Downloads only pixels inside the AOI boundary.
  - Precise Polygon Clipping: Auto-masks pixels outside the shapefile polygon.
  - Mosaicing: Automatically stitches overlapping tiles for a seamless output.
  - Platforms: Supports both Sentinel-2 (6 optical bands) and Sentinel-1 (SAR VV/VH bands).
=============================================================================
"""

import os, sys, logging, warnings, datetime, numpy as np, re, argparse
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
import rasterio.crs
from shapely.geometry import shape
import geopandas as gpd
from pystac_client import Client

# ── Setup Logging ──────────────────────────────────────────────────────────────
class NoEmojiFormatter(logging.Formatter):
    _e = re.compile("[" + u"\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
                    + u"\U0001F680-\U0001F6FF\U0001F700-\U0001FAFF"
                    + u"\U00002702-\U000027B0\U000024C2-\U0001F251" + "]+",
                    flags=re.UNICODE)
    def format(self, r): return self._e.sub('', super().format(r))

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
BASE_DIR   = Path(r"C:\Users\RohanDhanajiGhadage\OneDrive - AIQ Space Ventures Private Limited\BACKUP\AIQ\SAR Data")
SHP_PATH   = BASE_DIR / "Mumbai_Flood_AOI" / "Mumbai_Flood_AOI.shp"
OUTPUT_DIR = BASE_DIR / "outputs"
LOG_DIR    = BASE_DIR / "logs"

# ══════════════════════════════════════════════════════════════════════════════
# DIRECT WARP FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

import urllib.request

def fetch_and_warp_band_direct(item, band_name, dst_transform, dst_width, dst_height, dst_crs):
    """
    Directly warp the band from S3/HTTPS over the network.
    Must be run sequentially in the main thread to avoid Windows PROJ database thread-safety issues.
    Optimized to only reproject the overlapping sub-window, avoiding slow out-of-bounds S3 range requests.
    """
    if band_name not in item.assets:
        jp2_name = f"{band_name}-jp2"
        if jp2_name in item.assets:
            return fetch_and_warp_band_direct(item, jp2_name, dst_transform, dst_width, dst_height, dst_crs)
        return band_name, None

    url = item.assets[band_name].href
    if url.startswith("s3://"):
        bucket_and_key = url[5:]
        bucket, key = bucket_and_key.split("/", 1)
        url = f"https://{bucket}.s3.amazonaws.com/{key}"

    clean_name = band_name.replace('-jp2', '')
    max_retries = 3
    import time
    for attempt in range(1, max_retries + 1):
        try:
            # Append attempt query parameter to bypass GDAL's persistent VSI cache on retries
            attempt_url = f"{url}&attempt={attempt}" if "?" in url else f"{url}?attempt={attempt}"
            logging.getLogger("SatFetcher").info(f"    Streaming and warping {clean_name}... (Attempt {attempt}/{max_retries})")
            with rasterio.Env(
                AWS_NO_SIGN_REQUEST='YES',
                GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif,.tiff,.jp2',
                GDAL_HTTP_TIMEOUT=30,
                GDAL_HTTP_RETRY_COUNT=3,
                GDAL_HTTP_RETRY_DELAY=5,
                GDAL_CACHEMAX=512,
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES='YES',
                VSI_CACHE='TRUE',
                VSI_CACHE_SIZE=104857600
            ):
                with rasterio.open(attempt_url) as src:
                    src_crs = src.crs
                    if src_crs is None:
                        # Skip optimization for files with GCPs (like Sentinel-1) and run full canvas warp
                        band_dest = np.zeros((dst_height, dst_width), dtype='float32')
                        reproject(
                            source=rasterio.band(src, 1),
                            destination=band_dest,
                            src_transform=src.transform,
                            src_crs=None,
                            dst_transform=dst_transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.bilinear,
                            src_nodata=0,
                            dst_nodata=0
                        )
                        return clean_name, band_dest

                    # Try direct window read if CRS and resolution match (saves reproject overhead)
                    try:
                        dst_crs_obj = rasterio.crs.CRS.from_user_input(dst_crs)
                        src_res_x = src.transform[0]
                        src_res_y = -src.transform[4]
                        pixel_size = dst_transform[0]

                        if src_crs == dst_crs_obj and abs(src_res_x - pixel_size) < 1e-3 and abs(src_res_y - pixel_size) < 1e-3:
                            # Destination canvas bounds in target CRS
                            c_left, c_bottom, c_right, c_top = rasterio.transform.array_bounds(dst_height, dst_width, dst_transform)
                            # Source footprint bounds
                            s_left, s_bottom, s_right, s_top = src.bounds

                            # Intersection footprint
                            inter_left = max(s_left, c_left)
                            inter_right = min(s_right, c_right)
                            inter_bottom = max(s_bottom, c_bottom)
                            inter_top = min(s_top, c_top)

                            if inter_left >= inter_right or inter_bottom >= inter_top:
                                return clean_name, None

                            # Window in source coordinate grid
                            src_window = rasterio.windows.from_bounds(
                                inter_left, inter_bottom, inter_right, inter_top, src.transform
                            )
                            # Read window from source
                            sub_dest = src.read(1, window=src_window).astype('float32')

                            # Row/col in destination canvas
                            row_start, col_start = rasterio.transform.rowcol(dst_transform, inter_left, inter_top)
                            row_end, col_end = rasterio.transform.rowcol(dst_transform, inter_right, inter_bottom)

                            # Clamp to destination boundaries
                            row_start = max(0, min(dst_height - 1, row_start))
                            row_end = max(0, min(dst_height, row_end))
                            col_start = max(0, min(dst_width - 1, col_start))
                            col_end = max(0, min(dst_width, col_end))

                            sub_width = col_end - col_start
                            sub_height = row_end - row_start

                            if sub_width > 0 and sub_height > 0:
                                if sub_dest.shape != (sub_height, sub_width):
                                    import scipy.ndimage
                                    sub_dest = scipy.ndimage.zoom(
                                        sub_dest,
                                        (sub_height / sub_dest.shape[0], sub_width / sub_dest.shape[1]),
                                        order=1
                                    )
                                band_dest = np.zeros((dst_height, dst_width), dtype='float32')
                                band_dest[row_start:row_end, col_start:col_end] = sub_dest
                                return clean_name, band_dest
                    except Exception as e_direct:
                        logging.getLogger("SatFetcher").debug(f"Direct window read skipped: {e_direct}")

                    try:
                        # Calculate tile footprint and intersection
                        src_left, src_bottom, src_right, src_top = src.bounds
                        xs, ys = rasterio.warp.transform(src_crs, dst_crs, [src_left, src_right], [src_bottom, src_top])
                        t_left, t_right = min(xs), max(xs)
                        t_bottom, t_top = min(ys), max(ys)

                        c_left, c_bottom, c_right, c_top = rasterio.transform.array_bounds(dst_height, dst_width, dst_transform)
                        inter_left = max(t_left, c_left)
                        inter_right = min(t_right, c_right)
                        inter_bottom = max(t_bottom, c_bottom)
                        inter_top = min(t_top, c_top)

                        if inter_left >= inter_right or inter_bottom >= inter_top:
                            return clean_name, None

                        # Find pixel coordinates on canvas
                        row_start, col_start = rasterio.transform.rowcol(dst_transform, inter_left, inter_top)
                        row_end, col_end = rasterio.transform.rowcol(dst_transform, inter_right, inter_bottom)

                        # Clamp to canvas boundaries
                        row_start = max(0, min(dst_height - 1, row_start))
                        row_end = max(0, min(dst_height, row_end))
                        col_start = max(0, min(dst_width - 1, col_start))
                        col_end = max(0, min(dst_width, col_end))

                        sub_width = col_end - col_start
                        sub_height = row_end - row_start

                        if sub_width <= 0 or sub_height <= 0:
                            return clean_name, None

                        # Build sub-transform
                        sub_transform = rasterio.transform.from_bounds(
                            inter_left, inter_bottom, inter_right, inter_top, sub_width, sub_height
                        )

                        # Warp only the intersecting window
                        sub_dest = np.zeros((sub_height, sub_width), dtype='float32')
                        reproject(
                            source=rasterio.band(src, 1),
                            destination=sub_dest,
                            src_transform=src.transform,
                            src_crs=src_crs,
                            dst_transform=sub_transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.bilinear,
                            src_nodata=0,
                            dst_nodata=0
                        )

                        band_dest = np.zeros((dst_height, dst_width), dtype='float32')
                        band_dest[row_start:row_end, col_start:col_end] = sub_dest
                        return clean_name, band_dest

                    except Exception as e_opt:
                        # Fallback to standard full-canvas warp
                        logging.getLogger("SatFetcher").debug(f"Sub-window warp failed: {e_opt}. Running fallback full-canvas warp...")
                        band_dest = np.zeros((dst_height, dst_width), dtype='float32')
                        reproject(
                            source=rasterio.band(src, 1),
                            destination=band_dest,
                            src_transform=src.transform,
                            src_crs=src_crs,
                            dst_transform=dst_transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.bilinear,
                            src_nodata=0,
                            dst_nodata=0
                        )
                        return clean_name, band_dest
        except Exception as e:
            logging.getLogger("SatFetcher").warning(f"    [Attempt {attempt}/{max_retries}] Error warping '{band_name}': {e}")
            if attempt < max_retries:
                logging.getLogger("SatFetcher").info("    Waiting 5 seconds before retrying...")
                time.sleep(5)
            else:
                logging.getLogger("SatFetcher").error(f"    Failed after {max_retries} attempts.")
                return clean_name, None

# ══════════════════════════════════════════════════════════════════════════════
# PROCESSING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_fetch_pipeline(shp_path, output_dir, platform, start_date, end_date, pixel_size=10.0):
    logger = logging.getLogger("SatFetcher")
    
    # ── 1. Load Shapefile and Determine Projection ────────────────────────────
    logger.info(f"Loading Shapefile: {shp_path}")
    if not Path(shp_path).exists():
        logger.error(f"Shapefile not found: {shp_path}")
        return
        
    gdf_wgs84 = gpd.read_file(shp_path).to_crs('epsg:4326')
    aoi_wgs84 = gdf_wgs84.union_all()
    
    # Dynamic UTM zone calculation from centroid longitude
    centroid = aoi_wgs84.centroid
    lon, lat = centroid.x, centroid.y
    utm_zone = int((lon + 180) / 6) + 1
    
    if lat >= 0:
        target_epsg = 32600 + utm_zone
    else:
        target_epsg = 32700 + utm_zone
        
    logger.info(f"Centroid Coord: Lon={lon:.4f}, Lat={lat:.4f}")
    logger.info(f"Auto-selected UTM Zone Projection: EPSG:{target_epsg} (UTM {utm_zone}{'N' if lat>=0 else 'S'})")
    
    # Reproject AOI to the selected UTM Zone
    gdf_utm = gdf_wgs84.to_crs(f'epsg:{target_epsg}')
    aoi_utm = gdf_utm.union_all()

    # ── 2. Configure Platforms, Bands & Subdirectories ────────────────────────
    if platform.lower() == 'sentinel-2':
        collection = "sentinel-2-l2a"
        bands = ['blue', 'green', 'red', 'nir', 'swir16', 'swir22']
        out_suffix = "S2_Optical"
        platform_dir = Path(output_dir) / "Sentinel-2 (Optical)"
    elif platform.lower() == 'sentinel-1':
        collection = "sentinel-1-grd"
        # Sentinel-1 polarizations: VV and VH are standard on land
        bands = ['vv', 'vh']
        out_suffix = "S1_SAR"
        platform_dir = Path(output_dir) / "Sentinel-1 (SAR)"
    else:
        logger.error("Unsupported platform! Choose 'sentinel-1' or 'sentinel-2'")
        return

    platform_dir.mkdir(parents=True, exist_ok=True)

    # ── 3. Build Target Canvas Grid ──────────────────────────────────────────
    minx, miny, maxx, maxy = aoi_utm.bounds
    dst_width  = int(np.ceil((maxx - minx) / pixel_size))
    dst_height = int(np.ceil((maxy - miny) / pixel_size))
    dst_transform = rasterio.transform.from_origin(minx, maxy, pixel_size, pixel_size)
    dst_crs = rasterio.crs.CRS.from_epsg(target_epsg).to_wkt()

    logger.info(f"Target Dimensions: {dst_width} cols x {dst_height} rows")
    logger.info(f"Target Resolution: {pixel_size} meters")

    canvas = np.zeros((len(bands), dst_height, dst_width), dtype='float32')

    # ── 4. STAC Query ────────────────────────────────────────────────────────
    logger.info("Connecting to Earth Search STAC API...")
    catalog = Client.open("https://earth-search.aws.element84.com/v1")
    
    bbox = list(aoi_wgs84.bounds)
    time_filter = f"{start_date.isoformat()}T00:00:00Z/{end_date.isoformat()}T23:59:59Z"
    logger.info(f"Searching STAC collection: '{collection}' | Range: {time_filter}")
    
    search = catalog.search(
        collections=[collection],
        bbox=bbox, datetime=time_filter, max_items=100)
    
    items = list(search.get_all_items())
    logger.info(f"Total overlapping scenes found: {len(items)}")

    # Filter strictly to the polygon boundary shape and select the minimum set of tiles using a greedy coverage algorithm
    items_with_geom = []
    for item in items:
        item_geom = shape(item.geometry)
        if item_geom.intersects(aoi_wgs84):
            inter_area = item_geom.intersection(aoi_wgs84).area
            items_with_geom.append((item, item_geom, inter_area))

    # Sort by overlap area descending
    items_with_geom.sort(key=lambda x: x[2], reverse=True)

    selected_items = []
    uncovered_aoi = aoi_wgs84
    original_area = aoi_wgs84.area

    for item, geom, _ in items_with_geom:
        if uncovered_aoi.is_empty:
            break
        # Calculate new area covered by this tile
        covered_new = uncovered_aoi.intersection(geom).area
        # If it covers more than 0.5% of the AOI, select it
        if covered_new > (original_area * 0.005):
            selected_items.append(item)
            uncovered_aoi = uncovered_aoi.difference(geom)

    overlapping_items = selected_items
    logger.info(f"Geometry-intersecting scenes: {len(items_with_geom)}")
    logger.info(f"Selected {len(overlapping_items)} scenes to cover the AOI (skipped redundant tiles).")

    if not overlapping_items:
        logger.warning("No scenes found matching the spatial AOI geometry.")
        return

    # ── 5. Warp and Stitch overlapping scenes ───────────────────────────────
    logger.info("\nStreaming satellite data to canvas...")
    for idx, item in enumerate(overlapping_items, 1):
        scene_id = item.id
        
        if platform.lower() == 'sentinel-2':
            cloud = item.properties.get('eo:cloud_cover', 100)
            logger.info(f"[{idx}/{len(overlapping_items)}] Stitching {scene_id} | Cloud: {cloud:.1f}%")
        else:
            logger.info(f"[{idx}/{len(overlapping_items)}] Stitching {scene_id}")

        # Fetch and reproject bands sequentially to avoid GDAL's multi-threading PROJ database issue on Windows
        tile_bands = {}
        for b in bands:
            b_name, band_data = fetch_and_warp_band_direct(item, b, dst_transform, dst_width, dst_height, f"epsg:{target_epsg}")
            if band_data is not None:
                tile_bands[b_name] = band_data

        if not tile_bands:
            continue

        # Generate valid data mask (pixels != 0 in any loaded band)
        ref_band = next(iter(tile_bands.values()))
        valid_mask = (ref_band != 0)

        # Draw to master canvas
        for b_idx, b_name in enumerate(bands):
            if b_name in tile_bands:
                canvas[b_idx][valid_mask] = tile_bands[b_name][valid_mask]

    # ── 6. Clip canvas precisely to shapefile geometry ────────────────────────
    logger.info("\nMasking out areas outside the shapefile polygon...")
    aoi_mask = geometry_mask(
        [aoi_utm],
        out_shape=(dst_height, dst_width),
        transform=dst_transform,
        invert=True
    )

    for idx in range(len(bands)):
        canvas[idx][~aoi_mask] = 0

    # ── 7. Save output GeoTIFF ───────────────────────────────────────────────
    shp_basename = Path(shp_path).stem
    output_filename = platform_dir / f"{shp_basename}_{out_suffix}_EPSG{target_epsg}.tif"
    logger.info(f"Writing master GeoTIFF to: {output_filename}")

    out_profile = {
        'driver': 'GTiff',
        'height': dst_height, 'width': dst_width,
        'count': len(bands), 'dtype': 'float32',
        'crs': dst_crs, 'transform': dst_transform,
        'compress': 'lzw', 'tiled': True,
        'blockxsize': 256, 'blockysize': 256,
        'predictor': 2, 'bigtiff': 'IF_SAFER', 'nodata': 0,
    }

    with Env():
        with rasterio.open(output_filename, 'w', **out_profile) as dst:
            for idx in range(len(bands)):
                dst.write(canvas[idx], idx + 1)
            for idx, name in enumerate(bands, 1):
                dst.set_band_description(idx, name)

    size_mb = output_filename.stat().st_size / (1024 * 1024)
    logger.info(f"Saved master file: {output_filename.name} ({size_mb:.1f} MB)")

    # ── 8. Save Metadata ─────────────────────────────────────────────────────
    mf = platform_dir / f"{shp_basename}_{out_suffix}_metadata.txt"
    finite = canvas[canvas != 0]
    with open(mf, 'w') as f:
        f.write(f"Satellite Data Fetch Metadata\n{'=' * 55}\n")
        f.write(f"Platform     : {platform}\n")
        f.write(f"Area / SHP   : {shp_basename} (Stitched & Polygon Clipped)\n")
        f.write(f"Date Range   : {start_date} to {end_date}\n")
        f.write(f"CRS          : EPSG:{target_epsg} (Auto-detected UTM)\n")
        f.write(f"Resolution   : {pixel_size:.1f} m\n")
        f.write(f"Bands ({len(bands):02d})  : {bands}\n")
        f.write(f"Shape        : {canvas.shape}  (bands, rows, cols)\n")
        if finite.size > 0:
            f.write(f"Value Range  : {finite.min():.2f} - {finite.max():.2f}\n")
            f.write(f"Mean / Std   : {finite.mean():.2f} / {finite.std():.2f}\n")
        f.write(f"File Size    : {size_mb:.1f} MB\n")
        f.write(f"Output File  : {output_filename.name}\n")
    logger.info(f"Metadata saved: {mf.name}")
    logger.info(f"--- FETCH PROCESS FOR {platform.upper()} COMPLETE ---")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Universal satellite downloader for Sentinel-1 and Sentinel-2")
    parser.add_argument("--shp", type=str, default=str(SHP_PATH), help="Path to input shapefile")
    parser.add_argument("--output", type=str, default=str(OUTPUT_DIR), help="Output directory")
    parser.add_argument("--platform", type=str, choices=["sentinel-1", "sentinel-2"], default="sentinel-2", 
                        help="Choose sentinel-1 (SAR) or sentinel-2 (optical)")
    parser.add_argument("--start", type=str, default="2025-05-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2025-05-01", help="End date (YYYY-MM-DD)")
    
    args = parser.parse_args()

    # Configure Console Logging
    logger = logging.getLogger("SatFetcher")
    logger.setLevel(logging.INFO)
    log_file = LOG_DIR / "sat_fetcher_run.log"
    ch = logging.StreamHandler(sys.stdout)
    fh = logging.FileHandler(log_file, encoding='utf-8')
    formatter = NoEmojiFormatter('%(asctime)s | %(levelname)s | %(message)s')
    ch.setFormatter(formatter)
    fh.setFormatter(formatter)
    logger.addHandler(ch)
    logger.addHandler(fh)

    try:
        start_date = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.datetime.strptime(args.end, "%Y-%m-%d").date()
    except Exception as e:
        logger.error(f"Invalid date format! Use YYYY-MM-DD: {e}")
        sys.exit(1)

    run_fetch_pipeline(
        shp_path=args.shp,
        output_dir=args.output,
        platform=args.platform,
        start_date=start_date,
        end_date=end_date
    )
