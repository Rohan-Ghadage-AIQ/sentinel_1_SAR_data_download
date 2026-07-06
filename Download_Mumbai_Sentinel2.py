"""
=============================================================================
Sentinel-2 Downloader for Mumbai Flood AOI
  - Clips to ACTUAL AOI polygon shape (not bounding box)
  - 6 bands: B2=Blue, B3=Green, B4=Red, B8=NIR, B11=SWIR1, B12=SWIR2
  - CRS: EPSG:32643 (WGS 84 / UTM zone 43N)
  - Date: 2026-06-01 to 2026-06-05
  - Output: SAR Data\outputs\
=============================================================================
"""

import os, logging, warnings, datetime, numpy as np, re, json
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
from rasterio.mask import mask as rio_mask
from rasterio.io import MemoryFile
import rasterio.transform
from shapely.geometry import shape, mapping
from shapely.ops import transform
import geopandas as gpd
from pystac_client import Client

try:
    from skimage.transform import resize
except ImportError:
    raise ImportError("Run: python -m pip install scikit-image")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
BASE_DIR   = Path(r"C:\Users\RohanDhanajiGhadage\OneDrive - AIQ Space Ventures Private Limited\BACKUP\AIQ\SAR Data")
SHP_PATH   = BASE_DIR / "Mumbai_Flood_AOI" / "Mumbai_Flood_AOI.shp"
OUTPUT_DIR = BASE_DIR / "outputs"
LOG_DIR    = BASE_DIR / "logs"

TARGET_EPSG = 32643  # WGS 84 / UTM zone 43N

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
log_file = LOG_DIR / f"mumbai_s2_{log_ts}.log"
logger   = logging.getLogger("MumbaiS2")
logger.setLevel(logging.INFO)
fmt = '%(asctime)s | %(levelname)s | %(message)s'
for h in [logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()]:
    h.setFormatter(NoEmojiFormatter(fmt)); logger.addHandler(h)

# ══════════════════════════════════════════════════════════════════════════════
# FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def download_band(item, band_name):
    """Download a single band from AWS. Returns (band_name, numpy_array) or None."""
    url = item.assets[band_name].href
    try:
        with rasterio.open(url) as src:
            data = src.read(1)
            logger.info(f"  Downloaded {band_name}  shape={data.shape}")
            return (band_name, data, src.transform, src.crs)
    except Exception as e:
        logger.error(f"  Failed to download {band_name}: {e}")
        return None


def download_and_clip_tile(item, aoi_geom_utm, aoi_geojson_utm, date_str, tile_label):
    """
    Download 6 bands for one tile, stack them, and clip to AOI POLYGON shape.
    Uses rasterio.mask.mask() for proper polygon clipping (NOT bounding box).
    """
    cloud = item.properties.get('eo:cloud_cover', '?')
    logger.info("=" * 70)
    logger.info(f"TILE: {tile_label}  DATE: {date_str}  cloud={cloud}%")
    logger.info(f"ITEM: {item.id}")
    logger.info("=" * 70)

    # ── Find available bands ────────────────────────────────────────────────
    exclude = {'visual', 'visual-jp2', 'thumbnail', 'tileinfo_metadata',
               'granule_metadata', 'product_metadata', 'overview', 'datastrip',
               'info', 'snow', 'cloud'}
    all_direct = [k for k in item.assets if not k.endswith('-jp2') and k not in exclude]
    all_jp2    = [k for k in item.assets if k.endswith('-jp2') and k not in exclude]
    pool = all_direct if all_direct else all_jp2

    available_bands = [b for b in pool if b in BAND_SELECTION]
    if not available_bands:
        available_bands = [b for b in all_jp2 if b.replace('-jp2', '') in BAND_SELECTION]
    if not available_bands:
        logger.error(f"None of {BAND_SELECTION} found!"); return None

    logger.info(f"Bands ({len(available_bands)}): {available_bands}")

    # ── Download all bands in parallel ──────────────────────────────────────
    band_results = {}
    with ThreadPoolExecutor(max_workers=min(6, len(available_bands))) as ex:
        futures = {ex.submit(download_band, item, b): b for b in available_bands}
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                bname, data, tfm, crs = result
                band_results[bname] = {'data': data, 'transform': tfm, 'crs': crs}

    if not band_results:
        logger.error("No bands downloaded!"); return None

    # ── Get reference info from first 10m band ──────────────────────────────
    # blue/green/red/nir are 10m, swir16/swir22 are 20m
    ref_band = None
    for b in ['blue', 'green', 'red', 'nir']:
        if b in band_results:
            ref_band = b; break
    if ref_band is None:
        ref_band = next(iter(band_results))

    ref_shape = band_results[ref_band]['data'].shape
    ref_transform = band_results[ref_band]['transform']
    ref_crs = band_results[ref_band]['crs']

    # Get EPSG reliably via pyproj
    try:
        native_epsg = pyproj.CRS.from_wkt(ref_crs.to_wkt()).to_epsg()
    except:
        native_epsg = ref_crs.to_epsg()
    logger.info(f"Native CRS: EPSG:{native_epsg}  ref={ref_band}  shape={ref_shape}")

    # ── Resample 20m bands to 10m ───────────────────────────────────────────
    loaded_names = []
    resampled = []
    for b in BAND_SELECTION:  # maintain order
        if b not in band_results:
            continue
        d = band_results[b]['data']
        if d.shape != ref_shape:
            logger.info(f"  Resampling {b}: {d.shape} -> {ref_shape}")
            d = resize(d, ref_shape, mode='reflect', preserve_range=True).astype(d.dtype)
        resampled.append(d.astype('float32'))
        loaded_names.append(b)

    if not resampled:
        logger.error("No bands after resampling!"); return None

    bands_array = np.stack(resampled, axis=0)
    logger.info(f"Stacked: {bands_array.shape} ({loaded_names})")

    # ── Write to MemoryFile, then clip with rasterio.mask ───────────────────
    # This clips to the ACTUAL POLYGON SHAPE, not bounding box
    n_bands, h, w = bands_array.shape
    crs_wkt = pyproj.CRS.from_epsg(TARGET_EPSG).to_wkt()

    logger.info("Clipping to AOI polygon shape...")
    with MemoryFile() as memfile:
        # Write full tile to memory
        profile = {
            'driver': 'GTiff', 'height': h, 'width': w,
            'count': n_bands, 'dtype': 'float32',
            'crs': ref_crs, 'transform': ref_transform, 'nodata': 0,
        }
        with memfile.open(**profile) as mem_dst:
            for i in range(n_bands):
                mem_dst.write(bands_array[i], i + 1)

        # Re-open and clip to polygon
        with memfile.open() as mem_src:
            # Transform AOI to the tile's native CRS if different
            if native_epsg != TARGET_EPSG:
                proj_fn = pyproj.Transformer.from_crs(
                    f'epsg:{TARGET_EPSG}', ref_crs, always_xy=True).transform
                clip_geom = transform(proj_fn, aoi_geom_utm)
                clip_shapes = [mapping(clip_geom)]
            else:
                clip_shapes = [aoi_geojson_utm]

            try:
                clipped_data, clipped_transform = rio_mask(
                    mem_src, clip_shapes, crop=True, nodata=0, all_touched=True)
                logger.info(f"Clipped to AOI: {clipped_data.shape}")
            except Exception as e:
                logger.error(f"Clip failed: {e}")
                return None

    # ── Write final GeoTIFF ─────────────────────────────────────────────────
    n_out, h_out, w_out = clipped_data.shape
    filename = OUTPUT_DIR / f"Mumbai_{date_str}_{tile_label}_6BAND_EPSG{TARGET_EPSG}.tif"

    out_profile = {
        'driver': 'GTiff',
        'height': h_out, 'width': w_out,
        'count': n_out, 'dtype': 'float32',
        'crs': crs_wkt, 'transform': clipped_transform,
        'compress': 'lzw', 'tiled': True,
        'blockxsize': 256, 'blockysize': 256,
        'predictor': 2, 'bigtiff': 'IF_SAFER', 'nodata': 0,
    }

    logger.info(f"Writing: {filename}")
    with Env(PROJ_LIB=PROJ_DIR, GDAL_DATA=PROJ_DIR):
        with rasterio.open(filename, 'w', **out_profile) as dst:
            for i in range(n_out):
                dst.write(clipped_data[i], i + 1)
            for i, name in enumerate(loaded_names, 1):
                dst.set_band_description(i, name)

    size_mb = filename.stat().st_size / (1024 * 1024)
    logger.info(f"Saved: {filename.name}  ({size_mb:.1f} MB)")

    # ── Write metadata ──────────────────────────────────────────────────────
    finite = clipped_data[clipped_data != 0]
    mf = OUTPUT_DIR / f"Mumbai_{date_str}_{tile_label}_6BAND_EPSG{TARGET_EPSG}_metadata.txt"
    with open(mf, 'w') as f:
        f.write(f"Sentinel-2 Metadata\n{'=' * 55}\n")
        f.write(f"Area         : Mumbai Flood AOI (polygon clipped)\n")
        f.write(f"Date         : {date_str}\n")
        f.write(f"Tile         : {item.id}\n")
        f.write(f"Tile Grid    : {tile_label}\n")
        f.write(f"Cloud Cover  : {cloud}%\n")
        f.write(f"CRS          : EPSG:{TARGET_EPSG} (WGS84 / UTM Zone 43N)\n")
        f.write(f"Pixel Size   : {clipped_transform.a:.3f} m\n")
        f.write(f"Bands ({len(loaded_names):02d})  : {loaded_names}\n")
        f.write(f"Shape        : {clipped_data.shape}  (bands, rows, cols)\n")
        if finite.size > 0:
            f.write(f"Value Range  : {finite.min():.2f} - {finite.max():.2f}\n")
            f.write(f"Mean / Std   : {finite.mean():.2f} / {finite.std():.2f}\n")
        f.write(f"File Size    : {size_mb:.1f} MB\n")
        f.write(f"Output File  : {filename.name}\n")
    logger.info(f"Metadata: {mf.name}")
    return str(filename)


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


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    logger.info(f"CRS: EPSG:{TARGET_EPSG}")
    logger.info(f"Date range: {START_DATE} to {END_DATE}")
    logger.info(f"Bands: {BAND_SELECTION}")

    # ── Load AOI ────────────────────────────────────────────────────────────
    logger.info(f"Loading AOI: {SHP_PATH}")
    gdf_wgs84 = gpd.read_file(SHP_PATH).to_crs('epsg:4326')
    gdf_utm   = gdf_wgs84.to_crs(f'epsg:{TARGET_EPSG}')

    aoi_wgs84 = gdf_wgs84.union_all()
    aoi_utm   = gdf_utm.union_all()
    aoi_geojson_utm = mapping(aoi_utm)  # GeoJSON dict for rasterio.mask

    logger.info(f"AOI type  : {aoi_wgs84.geom_type}")
    logger.info(f"AOI bounds (WGS84): {aoi_wgs84.bounds}")
    logger.info(f"AOI bounds (UTM)  : {aoi_utm.bounds}")
    logger.info(f"AOI area  : {aoi_utm.area / 1e6:.1f} sq km")

    # ── Search STAC ─────────────────────────────────────────────────────────
    logger.info("Connecting to Earth Search STAC...")
    catalog = Client.open("https://earth-search.aws.element84.com/v1")
    logger.info("Connected!")

    bbox = list(aoi_wgs84.bounds)
    time_filter = f"{START_DATE.isoformat()}T00:00:00Z/{END_DATE.isoformat()}T23:59:59Z"
    logger.info(f"Searching: {time_filter}")

    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox, datetime=time_filter, max_items=200)
    all_items = list(search.get_all_items())
    logger.info(f"Scenes found: {len(all_items)}")

    filtered = [it for it in all_items if shape(it.geometry).intersects(aoi_wgs84)]
    logger.info(f"Scenes overlapping AOI: {len(filtered)}")
    if not filtered:
        logger.error("No scenes found!"); return

    date_groups  = group_by_date(filtered)
    sorted_dates = sorted(date_groups)
    logger.info(f"\nDates found ({len(sorted_dates)}):")
    for d in sorted_dates:
        tiles  = [it.id.split('_')[1] for it in date_groups[d]]
        clouds = [round(it.properties.get('eo:cloud_cover', 999), 2) for it in date_groups[d]]
        logger.info(f"  {d} | {len(tiles)} tile(s) | {tiles} | cloud%={clouds}")

    # ── Download all tiles ──────────────────────────────────────────────────
    total = sum(len(v) for v in date_groups.values())
    logger.info(f"\nDownloading {total} tiles -> {OUTPUT_DIR}\n")

    saved = []
    n = 0
    for date in sorted_dates:
        for item in date_groups[date]:
            n += 1
            tile = item.id.split('_')[1] if '_' in item.id else item.id
            logger.info(f"\n[{n}/{total}]")
            out = download_and_clip_tile(
                item=item, aoi_geom_utm=aoi_utm,
                aoi_geojson_utm=aoi_geojson_utm,
                date_str=str(date), tile_label=tile)
            if out:
                saved.append(out)

    logger.info("\n" + "=" * 70)
    logger.info(f"COMPLETE | {len(saved)} / {total} files saved")
    logger.info(f"Output: {OUTPUT_DIR}")
    logger.info("=" * 70)
    for f in saved:
        logger.info(f"  {Path(f).name}")


if __name__ == "__main__":
    main()
