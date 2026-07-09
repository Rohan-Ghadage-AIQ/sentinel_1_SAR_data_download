# Sentinel-1 SLC Data Downloader & Processor

This folder contains the scripts and documentation for querying, downloading, and processing Sentinel-1 **Single-Look Complex (SLC)** data for the Mumbai Flood Area of Interest (AOI).

---

## 🚀 Single-Command Execution

You can perform the search, download, and stitching/cropping in **one single command** by passing the `--stitch` flag:

```powershell
.venv\Scripts\python SLC/download_SLC_image.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --start 2026-06-07 --end 2026-06-07 --bursts --username "your_username" --password "your_password" --stitch
```

### Command Parameters:
* `--shp`: Path to the Area of Interest (AOI) shapefile.
* `--start` & `--end`: Date range.
* `--bursts`: Downloads sub-swath bursts (~120MB each) rather than massive full scenes (~4-8GB each), saving 60% bandwidth.
* `--username` & `--password`: Your NASA Earthdata login credentials.
* `--stitch`: **(Recommended)** Automatically converts complex numbers to intensity, warps them from GCPs to UTM projection (`EPSG:32643`), stitches overlapping bursts, and crops the final mosaic exactly to your shapefile boundaries.

---

## 📡 Data Specifications & Resolution

### 1. Resolution
* **Output Mosaic Resolution**: The generated output GeoTIFF files have a resampled spatial resolution of **10 meters** (10m x 10m pixels).
* **Native Sensor Resolution**: The raw Sentinel-1 SLC sensor records data at a native resolution of **~3m in range** (across-track) and **~22m in azimuth** (along-track).

### 2. File Outputs
The output files are saved under `outputs/Sentinel-1 (SLC)/intensity/`:
* `Mumbai_Flood_AOI_S1_SLC_YYYY-MM-DD_VV_intensity.tif` (Stitched & cropped VV mosaic)
* `Mumbai_Flood_AOI_S1_SLC_YYYY-MM-DD_VH_intensity.tif` (Stitched & cropped VH mosaic)

---

## 🔍 Why are there lined gaps in the image?

If you inspect the raw stitched mosaics in QGIS, you will notice **diagonal/slanted dark lines** cutting through the image:

### The Reason:
1. **TOPSAR Acquisition Mode**: Sentinel-1 records SLC data using the TOPSAR (Terrain Observation with Progressive Scans SAR) mode. It sweeps the radar beam across three sub-swaths (IW1, IW2, IW3).
2. **Raw Guard Bands**: Each sub-swath is divided along the track into small slices called **bursts**. To prevent signal overlap and aliasing, the satellite records each burst with a small **black border (guard band)**.
3. **Debursting Requirement**: Raw SLC data is **un-debursted**. When we stitch the raw bursts together directly using spatial coordinates, the black guard bands show up as slanted lines.
4. **How to remove them**: In professional radar software (like ESA SNAP), you must run the **"Deburst"** operator. Debursting uses low-level phase synchronization metadata (orbit state vectors, burst timing offsets) to blend the overlaps and cut out the guard bands seamlessly.
