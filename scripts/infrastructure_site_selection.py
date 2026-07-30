#!/usr/bin/env python3
"""
Infrastructure Site Selection - Multi-criteria suitability analysis.

Combines slope, land cover, proximity to roads, and other criteria using
weighted overlay to identify optimal sites for infrastructure development.
"""

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Try pip-installed package first; fall back to local copy in repo root.
try:
    from _geoskill_data_fetcher import (add_bbox_date_args,
        parse_bbox_arg,
        parse_date_range_arg,
        DataFetcher,
        DataSource,
        BBox,
        DateRange,
        DataFetcherError,)
    _FETCHER_AVAILABLE = True
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _skill_dir = _Path(__file__).resolve().parent
    _repo_root = _skill_dir.parent.parent
    _local_fetcher = _repo_root / "_geoskill_data_fetcher"
    if _local_fetcher.exists():
        _sys.path.insert(0, str(_repo_root))
    from _geoskill_data_fetcher import (add_bbox_date_args,
        parse_bbox_arg,
        parse_date_range_arg,
        DataFetcher,
        DataSource,
        BBox,
        DateRange,
        DataFetcherError,)
    _FETCHER_AVAILABLE = True
except ImportError:  # pragma: no cover - graceful when running standalone
    _FETCHER_AVAILABLE = False



EXIT_OK = 0
EXIT_ARG = 2
EXIT_PROCESSING = 7


# Default criteria weights and scoring
DEFAULT_CRITERIA = {
    "slope": {"weight": 0.3, "preference": "low"},  # lower slope = better
    "landcover": {"weight": 0.2, "preference": "specific"},  # specific classes
    "roads_proximity": {"weight": 0.3, "preference": "close"},  # closer = better
    "elevation": {"weight": 0.2, "preference": "low"},  # lower = better
}


def generate_synthetic_data(out_dir, seed=42):
    """Generate 60x60 slope raster (center < 8°, outer 0-30°), 60x60 DEM, constraints GeoJSON."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import mapping, box
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.RandomState(seed)
    transform = from_origin(0, 60, 0.001, 0.001)
    H, W = 60, 60
    # Slope: lower in the middle, higher at edges (radial ramp 0-30°)
    yy, xx = np.mgrid[0:H, 0:W]
    cy, cx = H // 2, W // 2
    dist = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    max_d = np.sqrt(cy ** 2 + cx ** 2)
    slope = np.clip((dist / max_d) * 30.0 + rng.normal(0, 1.5, (H, W)), 0, 30).astype(np.float32)
    slope_p = out_dir / "slope_synthetic.tif"
    with rasterio.open(str(slope_p), "w", driver="GTiff", height=H, width=W,
                       count=1, dtype="float32", crs="EPSG:4326",
                       transform=transform) as dst:
        dst.write(slope, 1)
    # DEM: 100-500 m
    dem = rng.uniform(100, 500, (H, W)).astype(np.float32)
    dem_p = out_dir / "dem_synthetic.tif"
    with rasterio.open(str(dem_p), "w", driver="GTiff", height=H, width=W,
                       count=1, dtype="float32", crs="EPSG:4326",
                       transform=transform) as dst:
        dst.write(dem, 1)
    # Constraints: 3 polygon features excluding some areas
    constraints_features = []
    for k, (lo, hi) in enumerate([(0.005, 0.015), (0.020, 0.030), (0.040, 0.050)]):
        b = box(lo, lo, hi, hi)
        constraints_features.append({
            "type": "Feature",
            "properties": {"id": k, "type": "exclusion"},
            "geometry": mapping(b),
        })
    constraints_p = out_dir / "constraints_synthetic.geojson"
    constraints_p.write_text(json.dumps(
        {"type": "FeatureCollection", "features": constraints_features}, indent=2),
        encoding="utf-8")
    return slope_p, dem_p, constraints_p


def compute_suitability(slope_path: Path, landcover_path: Path = None,
                        roads_path: Path = None, elevation_path: Path = None,
                        criteria: Dict = None) -> Dict[str, Any]:
    """Compute weighted suitability score from multiple criteria rasters."""
    try:
        import numpy as np
        import rasterio
    except ImportError:
        return {"error": "rasterio/numpy not available"}

    with rasterio.open(slope_path) as ds:
        slope = ds.read(1).astype(np.float64)
        transform = ds.transform
        crs = ds.crs
        nodata = ds.nodata
        rows, cols = slope.shape

    if nodata is not None:
        valid = slope != nodata
    else:
        valid = np.ones_like(slope, dtype=bool)

    # Normalize slope (0-90 degrees -> 0-1, lower is better)
    slope_norm = np.clip(slope / 90.0, 0, 1)
    slope_score = 1 - slope_norm  # invert: low slope = high score

    # Start with slope score
    total_score = slope_score * DEFAULT_CRITERIA["slope"]["weight"]
    total_weight = DEFAULT_CRITERIA["slope"]["weight"]

    # Land cover score (simplified: lower class number = more suitable)
    if landcover_path and Path(landcover_path).exists():
        with rasterio.open(landcover_path) as ds:
            landcover = ds.read(1).astype(np.float64)
        # Assume classes: 1=barren, 2=grass, 3=crop, 4=forest, 5=urban, 6=water
        # Barren/grass most suitable
        lc_score = np.where(landcover <= 2, 1.0,
                           np.where(landcover <= 3, 0.7,
                                   np.where(landcover <= 4, 0.3, 0.0)))
        total_score += lc_score * DEFAULT_CRITERIA["landcover"]["weight"]
        total_weight += DEFAULT_CRITERIA["landcover"]["weight"]
        valid &= (landcover != nodata)

    # Roads proximity score (simplified: use a distance raster if provided)
    if roads_path and Path(roads_path).exists():
        with rasterio.open(roads_path) as ds:
            roads_dist = ds.read(1).astype(np.float64)
        # Closer to roads = higher score (normalize by max distance)
        max_dist = np.nanmax(roads_dist[roads_dist != nodata]) if nodata else np.nanmax(roads_dist)
        if max_dist > 0:
            roads_score = 1 - np.clip(roads_dist / max_dist, 0, 1)
        else:
            roads_score = np.zeros_like(roads_dist)
        total_score += roads_score * DEFAULT_CRITERIA["roads_proximity"]["weight"]
        total_weight += DEFAULT_CRITERIA["roads_proximity"]["weight"]

    # Normalize by total weight
    if total_weight > 0:
        suitability = total_score / total_weight
    else:
        suitability = slope_score

    suitability[~valid] = np.nan

    # Classify suitability
    high = int(np.sum((suitability >= 0.7) & valid))
    medium = int(np.sum((suitability >= 0.4) & (suitability < 0.7) & valid))
    low = int(np.sum((suitability < 0.4) & valid))
    total_valid = int(np.sum(valid))

    # Area
    if crs and crs.is_projected:
        pixel_area = abs(transform.a * transform.e)
    else:
        pixel_area = (abs(transform.a) * 111320) * (abs(transform.e) * 111320)

    return {
        "suitability_mean": round(float(np.nanmean(suitability)), 4),
        "high_suitability_pixels": high,
        "medium_suitability_pixels": medium,
        "low_suitability_pixels": low,
        "high_suitability_area_ha": round(high * pixel_area / 10000, 2),
        "medium_suitability_area_ha": round(medium * pixel_area / 10000, 2),
        "total_valid_pixels": total_valid,
    }


def generate_report(result: Dict, output_dir: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Site Selection Report</title>
<style>
body{{font-family:sans-serif;max-width:900px;margin:20px auto;padding:0 20px}}
h1{{color:#1a237e}}.summary{{background:#e8f5e9;padding:15px;border-radius:8px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #c8e6c9;padding:8px;text-align:left}}
th{{background:#c8e6c9}}
</style></head>
<body>
<h1>Infrastructure Site Selection Report</h1>
<p>Generated: {now}</p>
<div class="summary">
<table>
<tr><td>Mean suitability</td><td><strong>{result.get('suitability_mean', 0):.3f}</strong></td></tr>
<tr><td>High suitability</td><td><strong>{result.get('high_suitability_area_ha', 0)} ha</strong></td></tr>
<tr><td>Medium suitability</td><td><strong>{result.get('medium_suitability_area_ha', 0)} ha</strong></td></tr>
</table>
</div>
</body></html>"""
    (output_dir / "report.html").write_text(html, encoding="utf-8")
    (output_dir / "suitability-report.json").write_text(
        json.dumps({"timestamp": now, "results": result}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def auto_download_elevation(args, output_dir: Path) -> Dict[str, Any]:
    """Download one cop-dem-glo-30 scene from MPC using --bbox.

    cop-dem-glo-30 is a time-invariant DEM mosaic — the date range
    filter is omitted (passed as ``None``) so the STAC search doesn't
    reject the request.

    Returns metadata dict (also writes the path back to args.elevation).
    """
    if not _FETCHER_AVAILABLE:
        raise RuntimeError(
            "Shared data fetcher not importable. Pass --elevation <local.tif> instead, "
            "or ensure _geoskill_data_fetcher is on sys.path."
        )
    bbox = parse_bbox_arg(getattr(args, "bbox", None), getattr(args, "aoi_file", None))
    if bbox is None:
        raise RuntimeError("auto_download_elevation requires --bbox or --aoi-file")
    dr = parse_date_range_arg(getattr(args, "date_range", None))  # accepted for CLI; not used
    cache_dir = getattr(args, "cache_dir", None)
    fetcher = DataFetcher(
        source=DataSource.PLANETARY_COMPUTER,
        cache_dir=Path(cache_dir) if cache_dir else None,
    )
    items = fetcher.search_stac(
        collection="cop-dem-glo-30",
        bbox=bbox,
        date_range=None,  # cop-dem-glo-30 has no time dimension
        limit=1,
    )
    if not items:
        raise RuntimeError(
            f"No cop-dem-glo-30 items found in bbox={bbox}"
        )
    download_dir = output_dir / "downloaded"
    paths = fetcher.download_assets(
        items=items, out_dir=download_dir, max_items=1, max_total_mb=500,
        prefer_assets=['data'],
    )
    if not paths:
        raise RuntimeError("Download returned no files")
    args.elevation = str(paths[0])
    return {
        "data_source": "MPC",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "collection": "cop-dem-glo-30",
        "bbox": bbox.to_string(),
        "date_range": (f"{dr.start},{dr.end}" if dr else None),
        "n_items_searched": len(items),
        "downloaded_paths": [str(p) for p in paths],
    }


def run_site_selection(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir) if args.output_dir else Path("site-selection-output")

    # --- Auto-download mode: fetch cop-dem-glo-30 from MPC ---
    # DEM is time-invariant; date range is accepted for CLI consistency.
    fetch_meta = None
    if (getattr(args, "bbox", None) or getattr(args, "aoi_file", None)):
        if not getattr(args, "elevation", None):
            try:
                fetch_meta = auto_download_elevation(args, output_dir)
                mode = "auto_download"
                print(f"  Auto-downloaded elevation: {args.elevation}")
            except DataFetcherError as e:
                print(f"ERROR: auto-download failed: [{e.kind}] {e.message}", file=sys.stderr)
                return EXIT_PROCESSING if 'EXIT_PROCESSING' in dir() else 7
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.synthetic or not args.slope:
        mode = "synthetic"
        synth_dir = output_dir / "synthetic_input"
        slope_p, dem_p, _cons_p = generate_synthetic_data(synth_dir, seed=42)
        args.slope = str(slope_p)
        # Use synthetic DEM as elevation if --elevation not provided
        if args.elevation is None:
            args.elevation = str(dem_p)
        print(f"  Generated synthetic slope + DEM in {synth_dir}")
    else:
        mode = "file"
        if not Path(args.slope).exists():
            print(f"ERROR: Slope raster not found: {args.slope}", file=sys.stderr)
            return EXIT_ARG

    print("Computing suitability...")
    result = compute_suitability(
        Path(args.slope),
        Path(args.landcover) if args.landcover else None,
        Path(args.roads_proximity) if args.roads_proximity else None,
        Path(args.elevation) if args.elevation else None,
    )
    print(f"  Mean suitability: {result.get('suitability_mean', 0):.3f}")

    generate_report(result, output_dir)
    output_files = {
        "report.html": str(output_dir / "report.html"),
        "suitability-report.json": str(output_dir / "suitability-report.json"),
    }
    if mode == "synthetic":
        output_files["synthetic_input/slope_synthetic.tif"] = str(synth_dir / "slope_synthetic.tif")
        output_files["synthetic_input/dem_synthetic.tif"] = str(synth_dir / "dem_synthetic.tif")
        output_files["synthetic_input/constraints_synthetic.geojson"] = str(synth_dir / "constraints_synthetic.geojson")
    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "results": result,
        "output_files": output_files,
        "summary": {
            "mode": mode,
            "suitability_mean": result.get("suitability_mean"),
            "high_suitability_pixels": result.get("high_suitability_pixels"),
            "high_suitability_area_ha": result.get("high_suitability_area_ha"),
        },
        "parameters": {k: v for k, v in vars(args).items() if not k.startswith("_")},
    }
    ensure_t9_fields(manifest, args)
    # Auto-download provenance (only when --bbox/--aoi-file triggered a download)
    if fetch_meta is not None:
        manifest["data_source"] = fetch_meta.get("data_source")
        manifest["fetched_at"] = fetch_meta.get("fetched_at")
        manifest["collection"] = fetch_meta.get("collection")
        manifest["bbox"] = fetch_meta.get("bbox")
        manifest["date_range"] = fetch_meta.get("date_range")
    (output_dir / "output-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Output: {output_dir}")
    return EXIT_OK



def validate_args(args) -> int:
    """Validate file existence and numeric ranges.
    Returns exit code (0 = ok, 2 = arg error)."""
    if getattr(args, 'synthetic', False):
        return 0
    import sys
    from pathlib import Path
    file_args = {
        "slope": "args.slope",
        "landcover": "args.landcover",
    }
    for flag, accessor in file_args.items():
        path = eval(accessor)
        if path is not None and not Path(path).exists():
            print(f"ERROR: --{flag} not found: {path}", file=sys.stderr)
            return 2
    numeric_ranges = {
        "landcover": [0, 10],
    }
    for flag, (lo, hi) in numeric_ranges.items():
        val = getattr(args, flag, None)
        if val is None:
            continue
        if lo is not None and val < lo:
            print(f"ERROR: --{flag}={val} below minimum {lo}", file=sys.stderr)
            return 2
        if hi is not None and val > hi:
            print(f"ERROR: --{flag}={val} above maximum {hi}", file=sys.stderr)
            return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description="Infrastructure Site Selection")
    parser.add_argument("--slope", default=None, help="Slope raster (degrees, optional if --synthetic)")
    parser.add_argument("--landcover", help="Land cover raster")
    parser.add_argument("--roads-proximity", help="Roads proximity raster (distance)")
    parser.add_argument("--elevation", help="Elevation raster")
    parser.add_argument("--synthetic", action="store_true", help="Run with synthetic demo data")
    parser.add_argument("--output-dir", "-o", help="Output directory")
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")

    add_bbox_date_args(parser)
    args = parser.parse_args()
    rc = validate_args(args)
    if rc != 0:
        sys.exit(rc)
    try:
        sys.exit(run_site_selection(args))
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(EXIT_PROCESSING)




def ensure_t9_fields(manifest, args=None):
    """Inject 3 T9 fields (output_files, parameters/summary, timestamp) if missing."""
    injected = []
    if not isinstance(manifest, dict):
        return injected
    of_aliases = {"output_files", "files", "outputs", "artifacts", "products", "result_files"}
    ps_aliases = {"parameters", "summary", "params", "args", "inputs", "result", "results",
                  "stats", "metrics", "qc_summary", "findings"}
    ts_aliases = {"timestamp", "generated_at", "date", "created_at", "run_time",
                  "datetime", "time", "ts"}
    if not any(k in manifest for k in of_aliases):
        manifest["output_files"] = {}
        injected.append("output_files")
    if not any(k in manifest for k in ps_aliases):
        try:
            if args is not None:
                manifest["parameters"] = {
                    k: v for k, v in vars(args).items()
                    if not k.startswith("_") and not callable(v)
                }
            else:
                manifest["parameters"] = {"_info": "auto-injected"}
        except Exception:
            manifest["parameters"] = {"_info": "auto-injected"}
        injected.append("parameters")
    if not any(k in manifest for k in ts_aliases):
        from datetime import datetime as _dt, timezone as _tz
        manifest["timestamp"] = _dt.now(_tz.utc).isoformat()
        injected.append("timestamp")
    return injected
if __name__ == "__main__":
    main()
