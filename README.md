# Satellite Data Fetcher for India & Global AOIs

A unified, high-performance geospatial pipeline designed to query, stream, stitch, and mask Sentinel-1 (SAR) and Sentinel-2 (Optical) satellite imagery for any Area of Interest (AOI) shapefile globally.

---

## 📂 Git Directory Structure

This project is configured to keep the repository clean of massive binary outputs, temp caches, and local configurations. Ensure only the following files are pushed to GitHub:

```text
SAR Data/
├── Mumbai_Flood_AOI/                  # Input AOI Shapefile folder
│   ├── Mumbai_Flood_AOI.shp           # Shapefile geometry
│   ├── Mumbai_Flood_AOI.shx           # Shapefile index
│   ├── Mumbai_Flood_AOI.dbf           # Attribute table
│   └── Mumbai_Flood_AOI.prj           # Coordinate system details
├── SLC/
│   ├── download_SLC_image.py          # Sentinel-1 SLC downloader and stitcher
│   ├── README.md                      # Guide for Sentinel-1 SLC
│   └── Architecture.md                # Architecture for Sentinel-1 SLC
├── Fetch_AOI_Satellite_Data.py        # Universal pipeline script (Sentinel-1 & Sentinel-2 GRD)
├── Download_Mumbai_Sentinel2.py       # Unified Sentinel-2 downloader & stitcher
├── ARCHITECTURE.md                    # Technical details of optimizations
├── README.md                          # Set up & execution guide (this file)
├── requirements.txt                   # List of Python dependencies
└── .gitignore                         # Configured to ignore outputs/, cache/, & .venv/
```

*Note: Large output files (such as `.tif` layers inside `outputs/`), local virtual environments (`.venv/`), temp logs, and `.part` download caches are automatically ignored by the `.gitignore` configuration.*

---

## 🛠️ Step-by-Step Setup Guide (New PC Installation)

Follow these steps to set up and execute the pipeline on a new machine.

### Step 1: Clone the Repository
Clone the repository using Git and navigate to the project directory:
```powershell
git clone https://github.com/Rohan-Ghadage-AIQ/sentinel_1_SAR_data_download.git
cd "SAR Data"
```

### Step 2: Set Up a Virtual Environment
Create a clean isolated virtual environment inside the repository:
```powershell
python -m venv .venv
```

### Step 3: Activate the Virtual Environment
Activate the environment based on your operating system and shell:

* **Windows PowerShell (Recommended)**:
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
  *(If you get an execution policy error, run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process` first)*
* **Windows CMD**:
  ```cmd
  .venv\Scripts\activate.bat
  ```
* **Linux / macOS**:
  ```bash
  source .venv/bin/activate
  ```

### Step 4: Install Geospatial Dependencies
Install the required packages listed in `requirements.txt`:
```powershell
pip install -r requirements.txt
```

---

## 🚀 How to Run the Pipeline

The script `Fetch_AOI_Satellite_Data.py` is dynamic. It automatically loads your shapefile, computes its geographic centroid, determines the correct UTM zone projection (e.g. `EPSG:32643` for Mumbai), queries the STAC API, and clips the outputs exactly to your shapefile boundaries.

### 📅 Time-Series Support (Date Grouping)
* **Single Date**: If you run the script for a single day (e.g. `--start 2025-05-01 --end 2025-05-01`), it will save exactly one file for that date.
* **Date Ranges**: If you run the script for a multi-day range (e.g. `--start 2026-06-01 --end 2026-06-30`), the script automatically groups the scenes by date and outputs **a separate, clean GeoTIFF for every date the satellite passes over your area**. This allows you to easily view a historical time-series in QGIS.

### ⚙️ The `--resolution` Parameter (Controlling Speed & Data Usage)
You can specify the pixel spacing in meters (e.g., `10`, `20`, `40`, `80`) using the `--resolution` flag. 

* **Sentinel-1 (SAR)**: Raw files are untiled (striped), meaning GDAL must download nearly the entire 600 MB file at full resolution. **Sentinel-1's native physical resolution is 20m** (10m is just upsampled by the space agency). Setting resolution to 40m or 80m uses pre-built overview pyramids in memory, reducing download data by **16x to 64x** and saving hours of download time.
* **Sentinel-2 (Optical)**: Files are tiled COGs, allowing GDAL to only fetch the exact bounding crop box. While 10m is fast, running at 20m or 40m resolution makes downloads take **under 1–2 minutes** even on slow connections.

#### 📊 Download Benchmarks per Orbit Pass (at ~300 KB/s Standard Connection)

| Satellite Platform | 80m Resolution | 40m Resolution | 20m Resolution (Native S1) | 10m Resolution (Native S2) |
|---|---|---|---|---|
| **Sentinel-1 (SAR)** | **~1 minute** (~9 MB data) | **~3 minutes** (~37 MB data) | **~12 minutes** (~150 MB data) | ⚠️ **~2 hours** (~1.2 GB data) |
| **Sentinel-2 (Optical)** | **~30 seconds** (~5 MB data) | **~1.5 minutes** (~25 MB data) | **~5 minutes** (~100 MB data) | **~12 minutes** (~150 MB data) |

---

### 💻 Execution Examples

This repository contains three main entry-point scripts designed for different platforms and geospatial needs:

---

#### 1. Sentinel-1 (SAR Radar - Low-Level SLC Burst Stitcher)
If you need raw **Single-Look Complex (SLC)** amplitude data (e.g. for ground deformation or precise double-bounce urban backscatter analysis):
* **Single Command (Stitch & Crop)**:
  ```powershell
  python SLC/download_SLC_image.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --start 2026-06-01 --end 2026-06-30 --bursts --username "your_username" --password "your_password" --stitch
  ```
  *Note: Sub-swath burst downloading saves 60% bandwidth compared to full scene downloads. Mosaics are stored in `outputs/Sentinel-1 (SLC)/intensity/`.*

---

#### 2. Sentinel-2 (Optical - Unified S3-Direct Daily Mosaic Downloader)
If you need **Sentinel-2 optical data** (e.g. for true-color RGB maps or vegetation health analysis), this script warps tiles **directly from AWS S3 in memory**, requiring zero local disk space for raw files:
* **Run for the entire month (with Cloud Filter & Custom 20m Resolution)**:
  ```powershell
  python Download_Mumbai_Sentinel2.py --start 2026-06-01 --end 2026-06-30 --cloud-max 30 --resolution 20 --out-dir "outputs/Sentinel-2-June2026"
  ```
* **Run for a single day (Default 10m Resolution)**:
  ```powershell
  python Download_Mumbai_Sentinel2.py --start 2026-06-02 --end 2026-06-02 --out-dir "outputs/Sentinel-2-June2026"
  ```
  *Note: Use the `--resolution` parameter to dynamically change target pixel spacing in meters (e.g., `10`, `20`, `30`, `60`). Mosaics are saved directly as unified, cropped, 6-band daily GeoTIFFs.*

---

#### 3. General AOI Satellite Data Fetcher (Fast Window-Clipped GRD/L2A)
If you want to quickly download a region using standard remote sub-window clipping (GRD for Sentinel-1, L2A for Sentinel-2):
* **Sentinel-1 GRD (40m resolution)**:
  ```powershell
  python Fetch_AOI_Satellite_Data.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --platform sentinel-1 --start 2026-06-01 --end 2026-06-30 --resolution 40
  ```
* **Sentinel-2 Optical (20m resolution)**:
  ```powershell
  python Fetch_AOI_Satellite_Data.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --platform sentinel-2 --start 2026-06-01 --end 2026-06-30 --resolution 20
  ```

---

#### 4. Dynamic Shapefile Input (India & Global Level)
To process any other shapefile (e.g. a different state or region globally), just point to your custom shapefile:
```powershell
python Fetch_AOI_Satellite_Data.py --shp "path/to/your/new_area.shp" --platform sentinel-1 --start YYYY-MM-DD --end YYYY-MM-DD --resolution 40
```
*Note: The pipeline dynamically computes the UTM coordinate transformations and target projection zones automatically.*

---

## 💡 Important Best Practices & Troubleshooting

### 1. Sentinel-1 Orbit Repeat Constraints
* **Issue**: Running the script for Sentinel-1 on a specific date (e.g., May 1) might return `No scenes found matching the spatial AOI geometry.`
* **Reason**: Sentinel-1 has a 12-day repeat orbit cycle over India. The satellite passes directly over Mumbai on **May 7, 2025** (providing 99% coverage), but does not pass over it on May 1.
* **Best Practice**: If a single date returns zero scenes, expand your search range (e.g., `--start 2025-05-01 --end 2025-05-12`) to catch the orbital pass.

### 2. Network Stability & Retries
* **Resilience**: The script has a built-in 3-attempt retry loop with a 5-second backoff delay per band. To bypass GDAL's persistent cache on retry attempts (preventing corrupt handles from locking retries), an attempt query parameter (`?attempt=N`) is appended to the S3 URL on each retry.
* **HTTP Chunk Size**: GDAL configuration has been optimized to use standard 16 KB chunk sizes for window crops. This ensures only the exact pixels overlapping your shapefile boundary are requested, reducing overall bandwidth requirements by **20x** compared to full-tile downloads.

### 3. Visualization in QGIS
Once downloaded, drag the generated `.tif` files directly into QGIS:
* **Sentinel-2 (Optical)**:
  * **True Color RGB**: Set Band 3 as Red, Band 2 as Green, and Band 1 as Blue.
  * **False Color (Vegetation)**: Set Band 4 (NIR) as Red, Band 3 (Red) as Green, and Band 2 (Green) as Blue.
* **Sentinel-1 (SAR)**:
  * **Band 1** represents `vv` polarization.
  * **Band 2** represents `vh` polarization.
  * Create a false-color composite by mapping Red=VV, Green=VH, and Blue=VV/VH ratio.

---

## 📊 Band reference Table

| Band Index | Sentinel-2 (Optical) | Sentinel-1 (SAR) | Description |
|:---:|:---:|:---:|---|
| **Band 1** | `blue` (10m) | `vv` (10m) | Sentinel-2: Blue / Sentinel-1: Vertical polarization |
| **Band 2** | `green` (10m) | `vh` (10m) | Sentinel-2: Green / Sentinel-1: Cross polarization |
| **Band 3** | `red` (10m) | — | Sentinel-2: Red |
| **Band 4** | `nir` (10m) | — | Sentinel-2: Near-Infrared |
| **Band 5** | `swir16` (20m -> 10m) | — | Sentinel-2: SWIR-1 (Upscaled to 10m) |
| **Band 6** | `swir22` (20m -> 10m) | — | Sentinel-2: SWIR-2 (Upscaled to 10m) |

---

## 📚 Architectural Insights
For deep-dive details on how set-coverage greedy solvers, direct window read optimizations, Windows PROJ thread-safety, and GDAL VSI caching configurations are implemented in the backend, refer to [ARCHITECTURE.md](file:///C:/Users/RohanDhanajiGhadage/OneDrive%20-%20AIQ%20Space%20Ventures%20Private%20Limited/BACKUP/AIQ/SAR%20Data/ARCHITECTURE.md).
