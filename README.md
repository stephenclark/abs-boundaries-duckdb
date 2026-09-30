# ABS Boundaries DuckDB Loader

Standalone Python utility to download, cache and load Australian Bureau of Statistics boundary data into a DuckDB database with the Spatial extension enabled.

The script currently loads:

- SA2 2026, ASGS Edition 4
- LGA 2025, ASGS Edition 3
- UCL 2021, ASGS Edition 3

The differing vintages reflect the latest ABS releases available for each geography.

## Requirements

Python 3.10+ and DuckDB:

```bash
pip install duckdb
```

No GDAL, GeoPandas or other GIS libraries are required. Boundary files are read directly using DuckDB Spatial.

## Basic usage

Run with the defaults:

```bash
python abs_boundaries_duckdb.py
```

This creates:

```text
abs_boundaries.duckdb
```

and caches downloaded ABS files under:

```text
~/.cache/abs_boundaries/
```

## Specify database and cache locations

```bash
python abs_boundaries_duckdb.py \
    --db data/abs_boundaries.duckdb \
    --cache-dir data/cache/abs
```

## Load selected layers

Load only SA2:

```bash
python abs_boundaries_duckdb.py --layers sa2
```

Load SA2 and LGA:

```bash
python abs_boundaries_duckdb.py --layers sa2 lga
```

Valid layer names are:

```text
sa2
lga
ucl
```

## Reload existing database tables

Existing boundary tables are left unchanged by default.

To rebuild them from the cached ABS files:

```bash
python abs_boundaries_duckdb.py --reload
```

## Re-download ABS source files

To ignore cached ZIP files and download them again:

```bash
python abs_boundaries_duckdb.py --force-download
```

To download fresh source files and rebuild the database tables:

```bash
python abs_boundaries_duckdb.py \
    --force-download \
    --reload
```

## Database contents

The main tables are:

```text
abs_sa2
abs_lga
abs_ucl
abs_boundary_sources
```

`abs_boundary_sources` records provenance including:

- geography type
- reference year
- ASGS edition
- datum and EPSG
- ABS source URL
- downloaded filename
- SHA256 file hash
- row count
- download/load timestamps

Geometry-free convenience views are also created:

```text
abs_sa2_meta
abs_lga_meta
abs_ucl_meta
```

A common cross-geography view is available as:

```text
abs_geographies
```

## Example queries

List Queensland SA2s:

```sql
SELECT
    sa2_code,
    sa2_name,
    area_sqkm
FROM abs_sa2
WHERE state_code = '3'
ORDER BY sa2_name;
```

Find the SA2 containing a longitude/latitude point:

```sql
SELECT
    sa2_code,
    sa2_name
FROM abs_sa2
WHERE ST_Contains(
    geom,
    ST_Point(153.0251, -27.4698)
);
```

Inspect loaded source vintages:

```sql
SELECT
    geography_type,
    reference_year,
    asgs_edition,
    row_count,
    source_file
FROM abs_boundary_sources
ORDER BY geography_type;
```

## Caching and idempotency

The script is safe to run repeatedly.

By default it:

1. reuses downloaded ZIP files,
2. reuses extracted shapefiles,
3. leaves existing DuckDB boundary tables unchanged,
4. validates the loaded tables, and
5. updates source metadata.

Use `--reload` when the database tables need rebuilding and `--force-download` when the ABS source files need refreshing.

## Spatial indexing

Each boundary table receives:

- an index on its geography code, and
- an R-tree index on `geom`.

These support both ordinary geography lookups and spatial predicates such as `ST_Contains`, `ST_Within` and `ST_Intersects`.

## Envoronment
Create and avtivate environment
```bash
python3 -m venv venv

#Activate
source venv/bin/activate

python -m pip install -r requirements.txt
pip install --upgrade pip

```
