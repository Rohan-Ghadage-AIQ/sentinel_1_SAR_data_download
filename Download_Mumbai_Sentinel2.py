"""
=============================================================================
Sentinel-2 Daily Mosaic Downloader & Processor for Mumbai Flood AOI
  - Warps bands directly from AWS S3 (extremely fast, zero intermediate disk space)
  - Groups scenes by date and creates separate daily mosaics
  - Clips precisely to the Mumbai AOI polygon shape
  - 6 bands: B2=Blue, B3=Green, B4=Red, B8=NIR, B11=SWIR1, B12=SWIR2
  - CRS: EPSG:32643 (WGS 84 / UTM zone 43N)
=============================================================================
"""

import os
import sys
import argparse
import logging
import warnings
import datetime
import re
import numpy as np
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings('ignore')

# ── Fix PROJ / GDAL environment ───────────────────────────────────────────────
os.environ['PROJ_NETWORK']        = 'OFF'
os.environ['PROJ_DEBUG']          = '0'
os.environ['AWS_NO_SIGN_REQUEST'] = 'YES'

import rasterio
import rasterio.crs
from rasterio.env import Env
from rasterio.warp import reproject, Resampling
from rasterio.features import geometry_mask
import rasterio.transform
from shapely.geometry import shape, mapping
import geopandas as gpd
from pystac_client import Client

# Set up logging
logger = logging.getLogger("MumbaiS2Mosaic")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
    h_stream = logging.StreamHandler(sys.stdout)
    h_stream.setFormatter(fmt)
    logger.addHandler(h_stream)

# Config constants
TARGET_EPSG = 32643  # WGS 84 / UTM zone 43N
PIXEL_SIZE  = 10.0   # 10 meters resolution
BAND_SELECTION = ['blue', 'green', 'red', 'nir', 'swir16', 'swir22']

def warp_band_to_grid(item, band_name, dst_transform, dst_width, dst_height, dst_crs):
    """
    Directly warps a single band from the STAC asset URL into the destination grid.
    This requests only the necessary pixels from S3 on the fly.
    """
    url = item.assets[band_name].href
    try:
        with Env(AWS_NO_SIGN_REQUEST='YES'):
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

def group_by_date(items):
    groups = {}
    for item in items:
        try:
            dt = datetime.datetime.strptime(item.id.split('_')[2], '%Y%m%d').date()
        except:
            dt = datetime.datetime.strptime(
                item.properties.get('datetime', '')[:10], '%Y-%m-%d').date()
        groups.setdefault(dt, []).append(item)
    return groups

def main():
    global PIXEL_SIZE
    parser = argparse.ArgumentParser(description="Query, download, and stitch Sentinel-2 data for Mumbai AOI.")
    parser.add_argument("--shp", default="Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp", help="Path to the AOI shapefile.")
    parser.add_argument("--start", default="2026-06-01", help="Start date (YYYY-MM-DD).")
    parser.add_argument("--end", default="2026-06-05", help="End date (YYYY-MM-DD).")
    parser.add_argument("--out-dir", default="outputs/Sentinel-2-Mosaics", help="Directory to save output mosaics.")
    parser.add_argument("--cloud-max", type=float, default=100.0, help="Exclude tiles with cloud cover greater than this percentage.")
    parser.add_argument("--resolution", type=float, default=10.0, help="Target pixel resolution in meters (e.g. 10, 20, 30). Default is 10.0.")
    
    args = parser.parse_args()
    PIXEL_SIZE = args.resolution
    
    # Setup directories
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Load dates
    try:
        start_date = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.datetime.strptime(args.end, "%Y-%m-%d").date()
    except Exception as e:
        logger.error(f"Error parsing dates: {e}")
        sys.exit(1)
        
    # Load shapefile
    if not os.path.exists(args.shp):
        logger.error(f"Shapefile not found: {args.shp}")
        sys.exit(1)
        
    logger.info(f"Loading AOI: {args.shp}")
    gdf_wgs84 = gpd.read_file(args.shp).to_crs('epsg:4326')
    aoi_wgs84 = gdf_wgs84.union_all()
    shp_basename = Path(args.shp).stem
    
    # Calculate target UTM projection from centroid longitude/latitude
    centroid = aoi_wgs84.centroid
    lon, lat = centroid.x, centroid.y
    utm_zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        target_epsg = 32600 + utm_zone
    else:
        target_epsg = 32700 + utm_zone

    logger.info("=" * 80)
    logger.info("STARTING SENTINEL-2 DAILY MOSAIC GENERATION")
    logger.info(f"Detected CRS  : EPSG:{target_epsg} (UTM Zone {utm_zone}{'N' if lat >= 0 else 'S'})")
    logger.info(f"Resolution    : {PIXEL_SIZE:.1f} m")
    logger.info(f"Bands        : {BAND_SELECTION}")
    logger.info(f"Date Range   : {start_date} to {end_date}")
    logger.info(f"Max Cloud %  : {args.cloud_max}%")
    logger.info("=" * 80)
    
    gdf_utm = gdf_wgs84.to_crs(f'epsg:{target_epsg}')
    aoi_utm = gdf_utm.union_all()
    
    # Define Destination Grid
    minx, miny, maxx, maxy = aoi_utm.bounds
    dst_width  = int(np.ceil((maxx - minx) / PIXEL_SIZE))
    dst_height = int(np.ceil((maxy - miny) / PIXEL_SIZE))
    dst_transform = rasterio.transform.from_origin(minx, maxy, PIXEL_SIZE, PIXEL_SIZE)
    dst_crs = rasterio.crs.CRS.from_epsg(target_epsg).to_wkt()
    
    logger.info(f"Grid size : {dst_width} cols x {dst_height} rows")
    
    # Generate binary polygon mask from shapefile
    logger.info("Generating polygon mask...")
    aoi_mask = geometry_mask(
        [aoi_utm],
        out_shape=(dst_height, dst_width),
        transform=dst_transform,
        invert=True  # True for pixels inside the polygon
    )
    
    # STAC Search
    logger.info("Connecting to Earth Search STAC...")
    catalog = Client.open("https://earth-search.aws.element84.com/v1")
    
    bbox = list(aoi_wgs84.bounds)
    time_filter = f"{start_date.isoformat()}T00:00:00Z/{end_date.isoformat()}T23:59:59Z"
    
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox, datetime=time_filter, max_items=200)
    
    items = list(search.get_all_items())
    logger.info(f"Found {len(items)} overlapping scenes total.")
    
    # Filter to only scenes that intersect our polygon
    intersecting_items = [it for it in items if shape(it.geometry).intersects(aoi_wgs84)]
    logger.info(f"Scenes overlapping AOI: {len(intersecting_items)}")
    
    if not intersecting_items:
        logger.error("No scenes found overlapping the AOI polygon. Exiting.")
        sys.exit(0)
        
    # Group by Date
    date_groups = group_by_date(intersecting_items)
    sorted_dates = sorted(date_groups.keys())
    
    logger.info(f"\nProcessing daily mosaics for {len(sorted_dates)} unique dates...")
    
    for date in sorted_dates:
        date_str = str(date)
        # Filter tiles for this date by cloud cover
        date_items = [
            item for item in date_groups[date]
            if item.properties.get('eo:cloud_cover', 100) <= args.cloud_max
        ]
        
        if not date_items:
            logger.info(f"\nSkipping date {date_str} - all tiles exceed cloud threshold of {args.cloud_max}%")
            continue
            
        logger.info("\n" + "-"*80)
        logger.info(f"DATE: {date_str} | Stitching {len(date_items)} tiles...")
        logger.info("-"*80)
        
        # Sort tiles by cloud cover DESCENDING (so clearest pixel overwrites cloudy ones on the canvas)
        date_items.sort(key=lambda x: x.properties.get('eo:cloud_cover', 100), reverse=True)
        
        # Initialize canvas arrays for this date
        canvas = np.zeros((len(BAND_SELECTION), dst_height, dst_width), dtype='float32')
        
        for idx, item in enumerate(date_items, 1):
            tile_id = item.id.split('_')[1] if '_' in item.id else item.id
            cloud = item.properties.get('eo:cloud_cover', 100)
            logger.info(f"  [{idx}/{len(date_items)}] Warping {tile_id} | Cloud: {cloud:.1f}%")
            
            # Warp all 6 bands sequentially from AWS
            tile_bands = {}
            for b in BAND_SELECTION:
                b_name, band_data = warp_band_to_grid(item, b, dst_transform, dst_width, dst_height, f"epsg:{target_epsg}")
                clean_name = b_name.replace('-jp2', '')
                if band_data is not None:
                    tile_bands[clean_name] = band_data
                        
            if not tile_bands:
                continue
                
            # Create a mask where this tile has valid data (pixels > 0 in any band)
            ref_band_data = tile_bands.get('blue', next(iter(tile_bands.values())))
            valid_tile_pixels = (ref_band_data > 0)
            
            # Paint the pixels onto our canvas
            for band_idx, b_name in enumerate(BAND_SELECTION):
                if b_name in tile_bands:
                    canvas[band_idx][valid_tile_pixels] = tile_bands[b_name][valid_tile_pixels]
                    
        # Apply the shapefile polygon mask (set outside to 0)
        for band_idx in range(len(BAND_SELECTION)):
            canvas[band_idx][~aoi_mask] = 0
            
        # Export final mosaic GeoTIFF
        output_filename = out_dir / f"{shp_basename}_Sentinel2_{date_str}_6BAND_EPSG{target_epsg}.tif"
        logger.info(f"  Saving stitched & cropped mosaic to: {output_filename.name}")
        
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
            with rasterio.open(output_filename, 'w', **out_profile) as dst:
                for band_idx in range(len(BAND_SELECTION)):
                    dst.write(canvas[band_idx], band_idx + 1)
                for band_idx, name in enumerate(BAND_SELECTION, 1):
                    dst.set_band_description(band_idx, name)
                    
        size_mb = output_filename.stat().st_size / (1024 * 1024)
        logger.info(f"  Successfully saved daily mosaic ({size_mb:.1f} MB)")
        
    logger.info("\n" + "="*80)
    logger.info("ALL SENTINEL-2 DAILY MOSAICS COMPLETED!")
    logger.info("="*80)

if __name__ == "__main__":
    main()
