import os
import sys
import argparse
import logging
import glob
import re
import numpy as np
import geopandas as gpd
import asf_search as asf
from shapely.geometry import mapping
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.features import rasterize


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("SLCDownloader")

def load_aoi_wkt(shp_path):
    if not os.path.exists(shp_path):
        logger.error(f"Shapefile not found: {shp_path}")
        sys.exit(1)
        
    logger.info(f"Loading Shapefile: {shp_path}")
    gdf = gpd.read_file(shp_path)
    
    # Ensure WGS84 coordinates for ASF Search API
    if gdf.crs != "epsg:4326":
        logger.info("Reprojecting AOI shapefile to WGS84 (EPSG:4326)...")
        gdf = gdf.to_crs("epsg:4326")
        
    # Combine all geometries to get a single unified WKT boundary
    unified_geom = gdf.geometry.union_all()
    wkt_geom = unified_geom.wkt
    logger.info("AOI WKT boundary successfully extracted.")
    return wkt_geom

def stitch_slc_bursts(input_dir, shp_path, date_str, target_crs="epsg:32643", pixel_size=10.0):
    logger.info("="*80)
    logger.info(f"Starting automatic stitching and cropping for date {date_str}...")
    logger.info("="*80)
    
    output_dir = os.path.join(input_dir, "intensity")
    os.makedirs(output_dir, exist_ok=True)
    
    # Load shapefile
    try:
        gdf = gpd.read_file(shp_path)
    except Exception as e:
        logger.error(f"Failed to load shapefile {shp_path}: {e}")
        return
        
    gdf_utm = gdf.to_crs(target_crs)
    minx, miny, maxx, maxy = gdf_utm.total_bounds
    
    # Pad bounds by 200m
    minx -= 200
    miny -= 200
    maxx += 200
    maxy += 200
    
    # Calculate canvas
    dst_width = int(np.ceil((maxx - minx) / pixel_size))
    dst_height = int(np.ceil((maxy - miny) / pixel_size))
    dst_transform = rasterio.transform.from_bounds(minx, miny, maxx, maxy, dst_width, dst_height)
    
    logger.info(f"Canvas size: {dst_width}x{dst_height} pixels at {pixel_size}m resolution")
    
    # Generate mask
    shapes = [(geom, 1) for geom in gdf_utm.geometry]
    polygon_mask = rasterize(
        shapes=shapes,
        out_shape=(dst_height, dst_width),
        transform=dst_transform,
        fill=0,
        dtype='uint8'
    )
    
    # Find all downloaded bursts for this date
    date_clean = date_str.replace("-", "")
    search_path = os.path.join(input_dir, f"*{date_clean}*.tiff")
    files = glob.glob(search_path)
    if not files:
        search_path = os.path.join(input_dir, f"*{date_clean}*.tif")
        files = glob.glob(search_path)
        
    # Exclude already processed intensity files
    files = [f for f in files if not os.path.basename(f).startswith("Mumbai_Flood_AOI_")]
    
    if not files:
        logger.warning(f"No downloaded bursts found for date {date_str} in '{input_dir}'")
        return
        
    logger.info(f"Found {len(files)} bursts to stitch.")
    
    vv_files = [f for f in files if "_VV_" in os.path.basename(f)]
    vh_files = [f for f in files if "_VH_" in os.path.basename(f)]
    
    groups = {"VV": vv_files, "VH": vh_files}
    
    out_profile = {
        'driver': 'GTiff',
        'dtype': 'float32',
        'nodata': 0.0,
        'width': dst_width,
        'height': dst_height,
        'count': 1,
        'crs': target_crs,
        'transform': dst_transform,
        'tiled': True,
        'blockxsize': 256,
        'blockysize': 256,
        'compress': 'lzw'
    }
    
    for pol, pol_files in groups.items():
        if not pol_files:
            continue
            
        logger.info(f"Stitching {len(pol_files)} bursts for polarization {pol}...")
        master_canvas = np.zeros((dst_height, dst_width), dtype=np.float32)
        count_canvas = np.zeros((dst_height, dst_width), dtype=np.float32)
        
        for idx, filepath in enumerate(pol_files, 1):
            filename = os.path.basename(filepath)
            try:
                with rasterio.open(filepath) as src:
                    data = src.read(1)
                    amplitude = np.abs(data).astype(np.float32)
                    
                    temp_dest = np.zeros((dst_height, dst_width), dtype=np.float32)
                    reproject(
                        source=amplitude,
                        destination=temp_dest,
                        gcps=src.gcps[0],
                        src_crs=src.gcps[1] or 'epsg:4326',
                        dst_transform=dst_transform,
                        dst_crs=target_crs,
                        resampling=Resampling.bilinear,
                        src_nodata=0,
                        dst_nodata=0
                    )
                    
                    valid_mask = (temp_dest > 0)
                    master_canvas[valid_mask] += temp_dest[valid_mask]
                    count_canvas[valid_mask] += 1
            except Exception as e:
                logger.error(f"  Error processing burst {filename}: {e}")
                
        overlap_mask = (count_canvas > 0)
        master_canvas[overlap_mask] /= count_canvas[overlap_mask]
        
        final_mosaic = master_canvas * polygon_mask
        
        out_filename = f"Mumbai_Flood_AOI_S1_SLC_{date_str}_{pol}_intensity.tif"
        out_path = os.path.join(output_dir, out_filename)
        
        logger.info(f"Saving final stitched & cropped {pol} mosaic to: {out_path}")
        with rasterio.open(out_path, 'w', **out_profile) as dst:
            dst.write(final_mosaic, 1)
            
    logger.info("Automatic stitching and cropping completed successfully!")
    logger.info("="*80)

def main():
    parser = argparse.ArgumentParser(description="Query and Download Sentinel-1 SLC/BURST data from ASF DAAC.")
    parser.add_argument("--shp", required=True, help="Path to the AOI shapefile.")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD).")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD).")
    parser.add_argument("--username", help="NASA Earthdata Login username.")
    parser.add_argument("--password", help="NASA Earthdata Login password.")
    parser.add_argument("--out-dir", default="outputs/Sentinel-1 (SLC)", help="Directory to save downloaded files.")
    parser.add_argument("--bursts", action="store_true", help="Download individual bursts (~100MB each) instead of full SLC scenes (~4-8GB each).")
    parser.add_argument("--dry-run", action="store_true", help="Search and display metadata without downloading.")
    parser.add_argument("--stitch", action="store_true", help="Automatically stitch and crop downloaded bursts into VV/VH mosaics.")
    
    args = parser.parse_args()
    
    # 1. Load WKT
    wkt_geom = load_aoi_wkt(args.shp)
    
    # 2. Setup output folder
    os.makedirs(args.out_dir, exist_ok=True)
    
    # 3. Determine product type
    product_type = asf.PRODUCT_TYPE.SLC
    if args.bursts:
        product_type = asf.PRODUCT_TYPE.BURST
        logger.info("Search Target: Sentinel-1 Single-Look Complex Bursts (SLC-BURST)")
    else:
        logger.info("Search Target: Sentinel-1 Single-Look Complex Full Scenes (SLC)")
        
    # 4. Perform Search
    start_dt = f"{args.start}T00:00:00Z"
    end_dt = f"{args.end}T23:59:59Z"
    logger.info(f"Querying ASF catalog from {start_dt} to {end_dt}...")
    try:
        results = asf.geo_search(
            platform=[asf.PLATFORM.SENTINEL1],
            processingLevel=product_type,
            intersectsWith=wkt_geom,
            start=start_dt,
            end=end_dt
        )
    except Exception as e:
        logger.error(f"Search query failed: {e}")
        sys.exit(1)
        
    total_found = len(results)
    logger.info(f"Total overlapping products found: {total_found}")
    
    if total_found == 0:
        logger.info("No scenes found matching the criteria. Exiting.")
        sys.exit(0)
        
    # Summarize results
    total_bytes = 0
    logger.info("\n" + "="*80)
    logger.info(f"{'Scene/Burst ID':<50} | {'Date (UTC)':<20} | {'Size (MB)':<10}")
    logger.info("="*80)
    
    for item in results:
        meta = item.properties
        size_mb = float(meta.get("bytes", 0)) / (1024 * 1024)
        total_bytes += int(meta.get("bytes", 0))
        scene_name = meta.get("sceneName", item.properties.get("fileID", "Unknown"))
        logger.info(f"{scene_name:<50} | {meta.get('startTime')[:19]:<20} | {size_mb:>8.1f} MB")
        
    total_gb = total_bytes / (1024 * 1024 * 1024)
    logger.info("="*80)
    logger.info(f"Total Download Size: {total_gb:.2f} GB")
    logger.info("="*80 + "\n")
    
    if args.dry_run:
        logger.info("Dry-run flag specified. Skipping download phase.")
        sys.exit(0)
        
    # 5. Handle Authentication
    logger.info("Setting up NASA Earthdata authentication...")
    session = asf.ASFSession()
    
    if args.username and args.password:
        try:
            session.auth_with_creds(args.username, args.password)
            logger.info("Successfully authenticated with provided credentials.")
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            sys.exit(1)
    else:
        logger.info("No credentials provided. Checking local .netrc file...")
        try:
            # asf_search will automatically look for .netrc if we don't pass session creds,
            # but we initialize the session here to verify it works early.
            # If no .netrc exists, this might raise or warn.
            session.auth_keypair()
            logger.info("Successfully authenticated using local keypair/.netrc.")
        except Exception:
            logger.warning("Local .netrc authentication check skipped or failed. "
                           "The download will proceed and asf_search will attempt auto-netrc fallback. "
                           "If it fails, please provide --username and --password.")
            
    # 6. Download files sequentially with retry and skip-existing check
    logger.info(f"Starting download of {total_found} products to '{args.out_dir}'...")
    failed_products = []
    
    for idx, product in enumerate(results, 1):
        meta = product.properties
        filename = meta.get("fileName", product.properties.get("fileID", "Unknown") + ".zip")
        url = meta.get("url", "")
        url_filename = os.path.basename(url.split("?")[0]) if url else filename
        
        file_path = os.path.join(args.out_dir, url_filename)
        expected_bytes = int(meta.get("bytes", 0))
        
        # Check if file already exists and is complete
        if os.path.exists(file_path):
            existing_bytes = os.path.getsize(file_path)
            # Allow minor size differences (e.g. metadata or file system block alignment differences)
            if abs(existing_bytes - expected_bytes) < 1024 * 1024 or existing_bytes > 0.95 * expected_bytes:
                logger.info(f"[{idx}/{total_found}] File already exists and is complete: {url_filename}. Skipping.")
                continue
                
        logger.info(f"[{idx}/{total_found}] Downloading {url_filename} ({expected_bytes / (1024*1024):.1f} MB)...")
        
        attempt = 1
        max_retries = 3
        success = False
        while attempt <= max_retries:
            try:
                product.download(path=args.out_dir, session=session)
                success = True
                break
            except Exception as e:
                logger.warning(f"  [Attempt {attempt}/{max_retries}] Error downloading {url_filename}: {e}")
                attempt += 1
                if attempt <= max_retries:
                    import time
                    time.sleep(5)
                
        if not success:
            logger.error(f"  Failed to download {url_filename} after {max_retries} attempts.")
            failed_products.append(url_filename)
            
    if failed_products:
        logger.error(f"Download finished with {len(failed_products)} failures:")
        for f in failed_products:
            logger.error(f"  - {f}")
        logger.error("You can re-run the command to retry downloading the failed products (it will automatically skip already completed downloads).")
        sys.exit(1)
    else:
        logger.info("All downloads completed successfully!")
        
        if args.stitch and args.bursts:
            # Extract unique dates from the downloaded products
            unique_dates = set()
            for product in results:
                meta = product.properties
                filename = meta.get("fileName", product.properties.get("fileID", ""))
                url = meta.get("url", "")
                url_filename = os.path.basename(url.split("?")[0]) if url else filename
                
                # Match 8 digits date prefix in standard filenames: S1_..._20260607T...
                match = re.search(r'_(\d{8})T', url_filename)
                if match:
                    d = match.group(1)
                    formatted_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                    unique_dates.add(formatted_date)
            
            if unique_dates:
                logger.info(f"Detected unique acquisition dates: {sorted(list(unique_dates))}")
                for date_str in sorted(list(unique_dates)):
                    stitch_slc_bursts(args.out_dir, args.shp, date_str)
            else:
                logger.warning("Could not extract unique dates from results. Running stitch for start date instead.")
                stitch_slc_bursts(args.out_dir, args.shp, args.start)

if __name__ == "__main__":
    main()
