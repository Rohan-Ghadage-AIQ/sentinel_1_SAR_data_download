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
├── Fetch_AOI_Satellite_Data.py        # Universal pipeline script (Sentinel-1 & Sentinel-2)
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

The script `Fetch_AOI_Satellite_Data.py` is dynamic. It automatically loads your shapefile, computes its geographic centroid, determines the correct UTM zone projection (e.g. `EPSG:32643` for Mumbai), queries the STAC API, merges all overlapping passes, and clips the mosaic exactly to the shapefile polygon boundaries.

### 1. Sentinel-1 (SAR Radar Imagery)
Downloads polarizations **VV and VH** (ideal for flood extent mapping, water body classification, and structural analysis).
```powershell
python Fetch_AOI_Satellite_Data.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --platform sentinel-1 --start 2025-05-07 --end 2025-05-07
```
* **Output Location**: `outputs/Sentinel-1 (SAR)/Mumbai_Flood_AOI_S1_SAR_EPSG32643.tif`
* **Size**: ~290 MB (depending on AOI shape).

### 2. Sentinel-2 (Optical Imagery)
Downloads 6 spectral bands: **Blue, Green, Red, NIR, SWIR1, and SWIR2** at 10m resolution (ideal for vegetation, true-color maps, and land cover classification).
```powershell
python Fetch_AOI_Satellite_Data.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --platform sentinel-2 --start 2025-05-01 --end 2025-05-01
```
* **Output Location**: `outputs/Sentinel-2 (Optical)/Mumbai_Flood_AOI_S2_Optical_EPSG32643.tif`

### 3. Dynamic Shapefile Input (India & Global Level)
To process any other shapefile (e.g. a different state or region in India):
```powershell
python Fetch_AOI_Satellite_Data.py --shp "path/to/your/new_area.shp" --platform sentinel-1 --start YYYY-MM-DD --end YYYY-MM-DD
```
*Note: The script dynamically handles coordinate transformations for any geographic location globally, auto-detecting the appropriate local UTM projection.*

---

## 💡 Important Best Practices & Troubleshooting

### 1. Sentinel-1 Orbit Repeat Constraints
* **Issue**: Running the script for Sentinel-1 on a specific date (e.g., May 1) might return `No scenes found matching the spatial AOI geometry.`
* **Reason**: Sentinel-1 has a 12-day repeat orbit cycle over India. The satellite passes directly over Mumbai on **May 7, 2025** (providing 99% coverage), but does not pass over it on May 1.
* **Best Practice**: If a single date returns zero scenes, expand your search range (e.g., `--start 2025-05-01 --end 2025-05-12`) to catch the orbital pass.

### 2. Network Stability & Retries
* **Resilience**: The script has a built-in 3-attempt retry loop with a 5-second backoff delay per band. If the connection to the remote Amazon S3 servers drops momentarily, the script will automatically reconnect and resume.
* **HTTP Chunk Size**: GDAL configuration has been optimized to request 1 MB chunks instead of 16 KB blocks. This reduces remote range-request queries by **8x**, making streaming highly stable even on slower connections.

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
