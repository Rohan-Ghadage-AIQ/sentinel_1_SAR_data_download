# Sentinel-1 SLC Data Download & Processing Report

This document outlines the technical specifications, file sizes, download times, and resolution constraints for downloading Sentinel-1 Single Look Complex (SLC) radar datasets.

---

## 📡 1. Resolution Specifications

### Slant Range Resolution (Native)
* **What is it?** SLC data is saved in the raw satellite radar geometry (slant range), not on a flat ground map.
* **Resolution**: The native resolution is approximately **2.7 meters to 3.5 meters** in the range direction, and **22 meters** in the azimuth (orbit direction).
* **Can we set it manually during download?** 
  * **No.** You cannot choose or change the resolution during download. You are downloading the raw raw complex values (amplitude and phase) exactly as recorded by the satellite sensor.
  * **Ground Resolution Mapping**: You can only set the resolution manually *after* downloading, when you run the data through a radar processor (like ESA SNAP or ISCE) to perform **Terrain Correction**. During this step, you can select your target ground resolution (e.g., 10m, 20m, or 40m).

---

## 💾 2. File Sizes & Bandwidth Consumption

To cover the Mumbai Area of Interest (AOI) on a single date, there are two download paths available:

### Path A: Full SLC Scenes (Standard Product)
Downloads the complete orbital swath packages.
* **Average File Size**: **~3.5 GB to 4 GB** per scene.
* **Total Scenes for Mumbai (June 7)**: 2 scenes.
* **Total Size**: **7.35 GB**

### Path B: Burst-Level Data (Optimized Product)
Downloads only the individual radar bursts (sub-swath slices) that directly intersect the Mumbai AOI shapefile.
* **Average File Size**: **~120 MB to 150 MB** per burst.
* **Total Bursts for Mumbai (June 7)**: 22 bursts.
* **Total Size**: **2.91 GB**

---

## ⏱️ 3. Download Time Estimates

The download duration depends heavily on your network connection speed:

| Product Path | Size | Speed: **50 KB/s** (Current Slow Network) | Speed: **10 MB/s** (100 Mbps Broadband) |
|---|---|---|---|
| **Single Burst** | ~130 MB | **~43 minutes** | **~13 seconds** |
| **All Bursts (Mumbai)** | 2.91 GB | ⚠️ **~16 hours** | **~5 minutes** |
| **Full SLC Scenes (Mumbai)** | 7.35 GB | ⚠️ **~40.8 hours** | **~12 minutes** |

---

## 💻 4. Running the Script

Ensure you are using the virtual environment `.venv` where all dependencies are installed.

### Verification (Dry-Run Search)
Shows available files and sizes without downloading:
```powershell
.venv\Scripts\python SLC\download_SLC_image.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --start 2026-06-07 --end 2026-06-07 --dry-run
```

### Download Bursts (Recommended for Speed)
```powershell
.venv\Scripts\python SLC\download_SLC_image.py --shp "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp" --start 2026-06-07 --end 2026-06-07 --bursts --username "your_username" --password "your_password"
```
