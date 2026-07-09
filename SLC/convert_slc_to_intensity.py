import os
import sys
import glob
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.features import rasterize

def main():
    input_dir = "outputs/Sentinel-1 (SLC)"
    output_dir = "outputs/Sentinel-1 (SLC)/intensity"
    os.makedirs(output_dir, exist_ok=True)
    
    # Load shapefile
    shp_path = "Mumbai_Flood_AOI/Mumbai_Flood_AOI.shp"
    if not os.path.exists(shp_path):
        print(f"Error: Shapefile not found at {shp_path}")
        sys.exit(1)
        
    print(f"Loading shapefile: {shp_path}")
    gdf = gpd.read_file(shp_path)
    target_crs = "epsg:32643"  # UTM Zone 43N (Mumbai region)
    gdf_utm = gdf.to_crs(target_crs)
    minx, miny, maxx, maxy = gdf_utm.total_bounds
    
    # Pad bounds slightly by 200m to ensure clean edges
    minx -= 200
    miny -= 200
    maxx += 200
    maxy += 200
    
    # Target resolution (10.0m matches native Sentinel-1 detail)
    pixel_size = 10.0
    dst_width = int(np.ceil((maxx - minx) / pixel_size))
    dst_height = int(np.ceil((maxy - miny) / pixel_size))
    dst_transform = rasterio.transform.from_bounds(minx, miny, maxx, maxy, dst_width, dst_height)
    
    print(f"Target Canvas Size: {dst_width} cols x {dst_height} rows")
    
    # Generate binary polygon mask from the shapefile
    print("Rasterizing shapefile geometries to generate clip mask...")
    shapes = [(geom, 1) for geom in gdf_utm.geometry]
    polygon_mask = rasterize(
        shapes=shapes,
        out_shape=(dst_height, dst_width),
        transform=dst_transform,
        fill=0,
        dtype='uint8'
    )
    
    # Find all Sentinel-1 SLC tiff files
    search_path = os.path.join(input_dir, "*.tiff")
    slc_files = glob.glob(search_path)
    if not slc_files:
        search_path = os.path.join(input_dir, "*.tif")
        slc_files = glob.glob(search_path)
        
    # Exclude already processed mosaic files
    slc_files = [
        f for f in slc_files 
        if not os.path.basename(f).startswith("Mumbai_Flood_AOI_")
    ]
    
    if not slc_files:
        print("No SLC files found to process. Exiting.")
        return
        
    # Group files by polarization (VV and VH)
    vv_files = [f for f in slc_files if "_VV_" in os.path.basename(f)]
    vh_files = [f for f in slc_files if "_VH_" in os.path.basename(f)]
    
    groups = {
        "VV": vv_files,
        "VH": vh_files
    }
    
    # Output metadata profile (standard GeoTIFF)
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
    
    for pol, files in groups.items():
        if not files:
            continue
            
        print(f"\nStitching and cropping {len(files)} files for polarization {pol}...")
        
        # Initialize canvas arrays for stitching
        master_canvas = np.zeros((dst_height, dst_width), dtype=np.float32)
        count_canvas = np.zeros((dst_height, dst_width), dtype=np.float32)
        
        for idx, filepath in enumerate(files, 1):
            filename = os.path.basename(filepath)
            print(f"  [{idx}/{len(files)}] Warping burst {filename}...")
            
            try:
                with rasterio.open(filepath) as src:
                    # Read complex64 band and calculate amplitude
                    data = src.read(1)
                    amplitude = np.abs(data).astype(np.float32)
                    
                    # Warp burst using its source GCPs to the UTM canvas
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
                    
                    # Accumulate overlapping pixels
                    valid_mask = (temp_dest > 0)
                    master_canvas[valid_mask] += temp_dest[valid_mask]
                    count_canvas[valid_mask] += 1
            except Exception as e:
                print(f"    Error processing {filename}: {e}")
                
        # Average overlapping burst boundary pixels to prevent double-brightness seams
        overlap_mask = (count_canvas > 0)
        master_canvas[overlap_mask] /= count_canvas[overlap_mask]
        
        # Crop exactly to the shapefile polygon mask
        final_mosaic = master_canvas * polygon_mask
        
        # Save output file
        out_path = os.path.join(output_dir, f"Mumbai_Flood_AOI_S1_SLC_2026-06-07_{pol}_intensity.tif")
        print(f"Saving final stitched & cropped {pol} mosaic to {out_path}...")
        with rasterio.open(out_path, 'w', **out_profile) as dst:
            dst.write(final_mosaic, 1)
            
    print("\n--- Processing and Stitching complete! ---")

if __name__ == "__main__":
    main()
