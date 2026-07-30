---
name: infrastructure-site-selection
description: >
  Multi-criteria suitability analysis for infrastructure site selection. Use when the user wants to analyze changes, compare multi-temporal
  rasters, compute indices, or generate assessment reports.
---

# Infrastructure Site Selection

Multi-criteria suitability analysis for infrastructure site selection.

## CLI Usage

```bash
python scripts/infrastructure_site_selection.py --slope slope.tif
python scripts/infrastructure_site_selection.py --slope slope.tif --landcover lc.tif --roads-proximity roads.tif
python scripts/infrastructure_site_selection.py --slope slope.tif --output-dir my_output
```

## Parameters

| Argument | Required | Default | Description |
|---|---|---|---|
| `--slope` | Yes | — | Path to slope raster (degrees) |
| `--landcover` | No | — | Optional land cover raster (categorical) |
| `--roads-proximity` | No | — | Optional distance-to-roads raster (m) |
| `--elevation` | No | — | Optional elevation raster (m) |
| `--output-dir`, `-o` | No | `site-selection-output` | Directory to write outputs |

## Output

| File | Description |
|---|---|
| `suitability-report.json` | Machine-readable suitability stats (mean, area by class) |
| `report.html` | Human-readable HTML report |
| `output-manifest.json` | Run metadata + result summary |

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Argument error (e.g. missing input file) |
| 7 | Processing failure |


## 数据下载

本 skill 可自动从 Microsoft Planetary Computer 下载数据 (无需 API key):

```bash
python infrastructure_site_selection.py --bbox 116,39,117,40 --date-range 2024-06-01,2024-06-30 --output-dir <tmp>
```

- `--bbox W,S,E,N`: WGS-84 边界框 (西, 南, 东, 北)
- `--date-range START,END`: 日期范围 (YYYY-MM-DD,YYYY-MM-DD)
- `--aoi-file <path.geojson>`: 替代 --bbox 的 GeoJSON 多边形
- `--cache-dir <path>`: 缓存目录 (默认 ~/.geoskill_cache)

当用户只给 `--bbox + --date-range` (没有 `--elevation`) 时，skill 自动下载数据。
当用户给 `--elevation` 时，走原文件路径 (向后兼容)。
