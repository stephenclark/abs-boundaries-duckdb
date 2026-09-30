#!/usr/bin/env python3
"""
abs_boundaries_duckdb.py

Download, cache, validate and load current ABS SA2, LGA and UCL boundaries
into a standalone DuckDB database using the DuckDB spatial extension.

Current boundary vintages as at 2026-09-30
------------------------------------------
SA2 : 2026, ASGS Edition 4
LGA : 2025, ASGS Edition 3
UCL : 2021, ASGS Edition 3

Design
------
* Downloads individual ABS Shapefile ZIPs.
* Caches downloads inside the project ./cache directory.
* Downloads atomically via .part files.
* Computes SHA256 hashes for provenance.
* Extracts each ZIP into its own cache directory.
* Uses DuckDB Spatial ST_Read() to ingest shapefiles.
* Converts ABS source columns into stable canonical column names.
* Stores geography dataset provenance separately from boundary rows.
* Creates ordinary indexes on geography codes.
* Creates R-tree indexes on geometry where supported.
* Validates geography-code uniqueness and geometry validity.
* Retains legitimate ABS non-spatial records with NULL geometry.
* Does not reload existing tables unless --reload is supplied.
* Does not re-download cached ZIPs unless --force-download is supplied.
* Allows individual layers to be selected with --layers.
* Creates convenient geometry-free views.
* Migrates the metadata table when new columns are introduced.

Requirements
------------
Python 3.10+

    pip install duckdb

Examples
--------
Load all layers:

    python abs_boundaries_duckdb.py

Only load SA2:

    python abs_boundaries_duckdb.py --layers sa2

Reload DuckDB tables from cached source files:

    python abs_boundaries_duckdb.py --reload

Force source files to be downloaded again:

    python abs_boundaries_duckdb.py --force-download

Download fresh files and rebuild tables:

    python abs_boundaries_duckdb.py --force-download --reload
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import urllib.request
import zipfile

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb


# =============================================================================
# Project paths
# =============================================================================

PROJECT_DIR = Path(__file__).resolve().parent


# =============================================================================
# Logging
# =============================================================================

LOG = logging.getLogger("abs_boundaries")


# =============================================================================
# ABS source configuration
# =============================================================================


@dataclass(frozen=True)
class BoundaryLayer:
    key: str
    table_name: str
    description: str
    reference_year: int
    asgs_edition: int
    datum: str
    epsg: int
    url: str
    expected_shapefile: str
    canonical_columns: tuple[tuple[str, str, str], ...]


ASGS_ED4_BASE = (
    "https://www.abs.gov.au/statistics/standards/"
    "australian-statistical-geography-standard-asgs/"
    "edition-4-july-2026-june-2031/"
    "access-and-downloads/digital-boundary-files"
)

ASGS_ED3_BASE = (
    "https://www.abs.gov.au/statistics/standards/"
    "australian-statistical-geography-standard-asgs/"
    "edition-3-july-2021-june-2026/"
    "access-and-downloads/digital-boundary-files"
)


LAYERS: dict[str, BoundaryLayer] = {
    "sa2": BoundaryLayer(
        key="sa2",
        table_name="abs_sa2",
        description="Statistical Area Level 2",
        reference_year=2026,
        asgs_edition=4,
        datum="GDA2020",
        epsg=7844,
        url=(
            f"{ASGS_ED4_BASE}/"
            "SA2_2026_AUST_SHP_GDA2020.zip"
        ),
        expected_shapefile="SA2_2026_AUST_GDA2020.shp",
        canonical_columns=(
            ("SA2_CODE26", "sa2_code", "VARCHAR"),
            ("SA2_NAME26", "sa2_name", "VARCHAR"),
            ("CHG_FLAG26", "change_flag", "VARCHAR"),
            ("CHG_LBL26", "change_label", "VARCHAR"),
            ("SA3_CODE26", "sa3_code", "VARCHAR"),
            ("SA3_NAME26", "sa3_name", "VARCHAR"),
            ("SA4_CODE26", "sa4_code", "VARCHAR"),
            ("SA4_NAME26", "sa4_name", "VARCHAR"),
            ("GCC_CODE26", "gccsa_code", "VARCHAR"),
            ("GCC_NAME26", "gccsa_name", "VARCHAR"),
            ("STE_CODE26", "state_code", "VARCHAR"),
            ("STE_NAME26", "state_name", "VARCHAR"),
            ("AUS_CODE26", "australia_code", "VARCHAR"),
            ("AUS_NAME26", "australia_name", "VARCHAR"),
            ("AREASQKM26", "area_sqkm", "DOUBLE"),
        ),
    ),

    "lga": BoundaryLayer(
        key="lga",
        table_name="abs_lga",
        description="Local Government Area",
        reference_year=2025,
        asgs_edition=3,
        datum="GDA2020",
        epsg=7844,
        url=(
            f"{ASGS_ED3_BASE}/"
            "LGA_2025_AUST_GDA2020.zip"
        ),
        expected_shapefile="LGA_2025_AUST_GDA2020.shp",
        canonical_columns=(
            ("LGA_CODE25", "lga_code", "VARCHAR"),
            ("LGA_NAME25", "lga_name", "VARCHAR"),
            ("STE_CODE21", "state_code", "VARCHAR"),
            ("STE_NAME21", "state_name", "VARCHAR"),
            ("AUS_CODE21", "australia_code", "VARCHAR"),
            ("AUS_NAME21", "australia_name", "VARCHAR"),
            ("AREASQKM", "area_sqkm", "DOUBLE"),
        ),
    ),

    "ucl": BoundaryLayer(
        key="ucl",
        table_name="abs_ucl",
        description="Urban Centre and Locality",
        reference_year=2021,
        asgs_edition=3,
        datum="GDA2020",
        epsg=7844,
        url=(
            f"{ASGS_ED3_BASE}/"
            "UCL_2021_AUST_GDA2020_SHP.zip"
        ),
        expected_shapefile="UCL_2021_AUST_GDA2020.shp",
        canonical_columns=(
            ("UCL_CODE21", "ucl_code", "VARCHAR"),
            ("UCL_NAME21", "ucl_name", "VARCHAR"),
            ("SSR_CODE21", "sosr_code", "VARCHAR"),
            ("SSR_NAME21", "sosr_name", "VARCHAR"),
            ("SOS_CODE21", "sos_code", "VARCHAR"),
            ("SOS_NAME21", "sos_name", "VARCHAR"),
            ("STE_CODE21", "state_code", "VARCHAR"),
            ("STE_NAME21", "state_name", "VARCHAR"),
            ("AUS_CODE21", "australia_code", "VARCHAR"),
            ("AUS_NAME21", "australia_name", "VARCHAR"),
            ("AREASQKM21", "area_sqkm", "DOUBLE"),
            ("LOCI_URI21", "asgs_loci_uri", "VARCHAR"),
        ),
    ),
}


# =============================================================================
# Utility functions
# =============================================================================


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while chunk := file.read(block_size):
            digest.update(chunk)

    return digest.hexdigest()


def utc_from_timestamp(timestamp: float) -> datetime:
    return (
        datetime
        .fromtimestamp(timestamp, timezone.utc)
        .replace(tzinfo=None)
    )


def quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def sql_string(value: str | Path) -> str:
    """
    Safely quote a string literal for SQL.
    """
    value = str(value)
    return "'" + value.replace("'", "''") + "'"


# =============================================================================
# Download
# =============================================================================


def download_file(
    url: str,
    destination: Path,
    force: bool = False,
) -> Path:

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        destination.exists()
        and destination.stat().st_size > 0
        and not force
    ):
        LOG.info("Using cached download: %s", destination)
        return destination

    temp_path = destination.with_suffix(
        destination.suffix + ".part"
    )

    if temp_path.exists():
        temp_path.unlink()

    LOG.info("Downloading:")
    LOG.info("  %s", url)
    LOG.info("  -> %s", destination)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; ABS-DuckDB-boundary-loader/1.0)"
            )
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=300,
        ) as response:
            with temp_path.open("wb") as output:
                shutil.copyfileobj(
                    response,
                    output,
                    length=1024 * 1024,
                )

        if temp_path.stat().st_size == 0:
            raise RuntimeError(
                f"Downloaded file is empty: {url}"
            )

        temp_path.replace(destination)

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    return destination


# =============================================================================
# Extraction
# =============================================================================


def find_expected_shapefile(
    extraction_dir: Path,
    expected_filename: str,
) -> Path | None:

    direct = extraction_dir / expected_filename

    if direct.exists():
        return direct

    matches = [
        path
        for path in extraction_dir.rglob("*.shp")
        if path.name.lower() == expected_filename.lower()
    ]

    if len(matches) == 1:
        return matches[0]

    return None


def extract_boundary(
    zip_path: Path,
    extraction_dir: Path,
    expected_filename: str,
    force: bool = False,
) -> Path:

    if force and extraction_dir.exists():
        shutil.rmtree(extraction_dir)

    if extraction_dir.exists():
        shp = find_expected_shapefile(
            extraction_dir,
            expected_filename,
        )

        if shp is not None:
            LOG.info("Using cached extraction: %s", shp)
            return shp

    LOG.info("Extracting: %s", zip_path)

    extraction_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extraction_dir)

    shp = find_expected_shapefile(
        extraction_dir,
        expected_filename,
    )

    if shp is None:
        available = sorted(
            str(path.name)
            for path in extraction_dir.rglob("*.shp")
        )

        raise FileNotFoundError(
            f"Expected shapefile {expected_filename!r} "
            f"not found in {zip_path}. "
            f"Available shapefiles: {available}"
        )

    return shp


# =============================================================================
# DuckDB
# =============================================================================


def connect_database(
    db_path: Path,
) -> duckdb.DuckDBPyConnection:

    db_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    con = duckdb.connect(str(db_path))

    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        LOG.info(
            "DuckDB Spatial extension not installed; installing it."
        )

        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")

    return con


def table_exists(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
) -> bool:

    row = con.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_name = ?
        )
        """,
        [table_name],
    ).fetchone()

    return bool(row[0])


# =============================================================================
# Metadata
# =============================================================================


def ensure_metadata_table(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """
    Create the metadata table if necessary and migrate older versions.
    """

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS abs_boundary_sources (
            geography_type      VARCHAR NOT NULL,
            table_name          VARCHAR NOT NULL,
            description         VARCHAR NOT NULL,

            reference_year      INTEGER NOT NULL,
            asgs_edition        INTEGER NOT NULL,

            datum               VARCHAR NOT NULL,
            epsg                INTEGER NOT NULL,

            source_url          VARCHAR NOT NULL,
            source_file         VARCHAR NOT NULL,
            source_sha256       VARCHAR NOT NULL,

            downloaded_at_utc   TIMESTAMP,
            loaded_at_utc       TIMESTAMP NOT NULL,

            row_count           BIGINT NOT NULL,
            non_spatial_count   BIGINT NOT NULL DEFAULT 0,

            PRIMARY KEY (
                geography_type,
                reference_year
            )
        )
        """
    )

    existing_columns = {
        row[1]
        for row in con.execute(
            """
            PRAGMA table_info('abs_boundary_sources')
            """
        ).fetchall()
    }

    if "non_spatial_count" not in existing_columns:
        LOG.info(
            "Migrating abs_boundary_sources: "
            "adding non_spatial_count"
        )

        con.execute(
            """
            ALTER TABLE abs_boundary_sources
            ADD COLUMN non_spatial_count BIGINT DEFAULT 0
            """
        )


# =============================================================================
# Source schema validation
# =============================================================================


def read_source_columns(
    con: duckdb.DuckDBPyConnection,
    shp_path: Path,
) -> dict[str, str]:

    shp_sql = sql_string(shp_path)

    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW _abs_source_schema AS
        SELECT *
        FROM ST_Read({shp_sql})
        LIMIT 0
        """
    )

    description = con.execute(
        "DESCRIBE _abs_source_schema"
    ).fetchall()

    return {
        str(row[0]).upper(): str(row[0])
        for row in description
    }


def detect_geometry_column(
    columns: dict[str, str],
) -> str:

    for candidate in (
        "GEOM",
        "WKB_GEOMETRY",
        "GEOMETRY",
    ):
        if candidate in columns:
            return columns[candidate]

    raise KeyError(
        "Could not identify geometry column. "
        f"Available columns: {sorted(columns.values())}"
    )


def validate_source_schema(
    con: duckdb.DuckDBPyConnection,
    layer: BoundaryLayer,
    shp_path: Path,
) -> tuple[dict[str, str], str]:

    columns = read_source_columns(
        con,
        shp_path,
    )

    missing: list[str] = []

    for source_name, _, _ in layer.canonical_columns:
        if source_name.upper() not in columns:
            missing.append(source_name)

    if missing:
        raise RuntimeError(
            f"{layer.key.upper()} source schema differs from expectation.\n"
            f"Missing expected fields: {missing}\n"
            f"Available fields: {sorted(columns.values())}"
        )

    geometry_column = detect_geometry_column(
        columns
    )

    return columns, geometry_column


# =============================================================================
# SQL construction
# =============================================================================


def build_select_list(
    layer: BoundaryLayer,
    source_columns: dict[str, str],
    geometry_column: str,
) -> str:

    expressions: list[str] = []

    for (
        source_name,
        destination_name,
        destination_type,
    ) in layer.canonical_columns:

        actual_source = source_columns[
            source_name.upper()
        ]

        expressions.append(
            f"CAST("
            f"{quote_identifier(actual_source)} "
            f"AS {destination_type}"
            f") AS "
            f"{quote_identifier(destination_name)}"
        )

    expressions.extend(
        [
            f"{layer.reference_year}::INTEGER "
            f"AS reference_year",

            f"{layer.asgs_edition}::INTEGER "
            f"AS asgs_edition",

            f"'{layer.datum}'::VARCHAR "
            f"AS datum",

            f"{layer.epsg}::INTEGER "
            f"AS epsg",

            f"{quote_identifier(geometry_column)} "
            f"AS geom",
        ]
    )

    return ",\n            ".join(expressions)


# =============================================================================
# Indexes
# =============================================================================


def canonical_code_column(
    layer: BoundaryLayer,
) -> str:

    return {
        "sa2": "sa2_code",
        "lga": "lga_code",
        "ucl": "ucl_code",
    }[layer.key]


def create_indexes(
    con: duckdb.DuckDBPyConnection,
    layer: BoundaryLayer,
) -> None:

    table = quote_identifier(
        layer.table_name
    )

    code_column = quote_identifier(
        canonical_code_column(layer)
    )

    normal_index = quote_identifier(
        f"idx_{layer.table_name}_code"
    )

    spatial_index = quote_identifier(
        f"idx_{layer.table_name}_geom"
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {normal_index}
        ON {table} ({code_column})
        """
    )

    try:
        con.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {spatial_index}
            ON {table}
            USING RTREE (geom)
            """
        )

    except duckdb.Error as exc:
        LOG.warning(
            "Could not create R-tree index for %s: %s",
            layer.table_name,
            exc,
        )


# =============================================================================
# Load
# =============================================================================


def load_layer(
    con: duckdb.DuckDBPyConnection,
    layer: BoundaryLayer,
    shp_path: Path,
    reload: bool,
) -> bool:

    exists = table_exists(
        con,
        layer.table_name,
    )

    if exists and not reload:
        LOG.info(
            "Table %s already exists; skipping load.",
            layer.table_name,
        )
        return False

    source_columns, geometry_column = (
        validate_source_schema(
            con,
            layer,
            shp_path,
        )
    )

    select_sql = build_select_list(
        layer,
        source_columns,
        geometry_column,
    )

    quoted_table = quote_identifier(
        layer.table_name
    )

    shp_sql = sql_string(
        shp_path
    )

    LOG.info(
        "Loading %s into %s",
        layer.description,
        layer.table_name,
    )

    con.execute("BEGIN")

    try:
        if exists:
            con.execute(
                f"DROP TABLE {quoted_table}"
            )

        con.execute(
            f"""
            CREATE TABLE {quoted_table} AS

            SELECT
                {select_sql}

            FROM ST_Read({shp_sql})
            """
        )

        create_indexes(
            con,
            layer,
        )

        con.execute("COMMIT")

    except Exception:
        con.execute("ROLLBACK")
        raise

    return True


# =============================================================================
# Validation
# =============================================================================


@dataclass(frozen=True)
class ValidationResult:
    row_count: int
    distinct_codes: int
    null_codes: int
    null_geometries: int
    empty_geometries: int
    invalid_geometries: int


def validate_loaded_layer(
    con: duckdb.DuckDBPyConnection,
    layer: BoundaryLayer,
) -> ValidationResult:

    table = quote_identifier(
        layer.table_name
    )

    code_column = quote_identifier(
        canonical_code_column(layer)
    )

    row = con.execute(
        f"""
        SELECT
            COUNT(*),

            COUNT(DISTINCT {code_column}),

            COUNT(*) FILTER (
                WHERE {code_column} IS NULL
            ),

            COUNT(*) FILTER (
                WHERE geom IS NULL
            ),

            COUNT(*) FILTER (
                WHERE geom IS NOT NULL
                  AND ST_IsEmpty(geom)
            ),

            COUNT(*) FILTER (
                WHERE geom IS NOT NULL
                  AND NOT ST_IsValid(geom)
            )

        FROM {table}
        """
    ).fetchone()

    result = ValidationResult(
        row_count=row[0],
        distinct_codes=row[1],
        null_codes=row[2],
        null_geometries=row[3],
        empty_geometries=row[4],
        invalid_geometries=row[5],
    )

    if (
        result.row_count
        != result.distinct_codes + result.null_codes
    ):
        raise RuntimeError(
            f"{layer.table_name}: duplicate geography codes detected."
        )

    if result.null_codes:
        raise RuntimeError(
            f"{layer.table_name}: "
            f"{result.null_codes} NULL geography codes detected."
        )

    if result.null_geometries:
        LOG.info(
            "%s: %d rows have NULL geometry "
            "(retained as ABS non-spatial records).",
            layer.table_name,
            result.null_geometries,
        )

    if result.empty_geometries:
        raise RuntimeError(
            f"{layer.table_name}: "
            f"{result.empty_geometries} empty geometries detected."
        )

    if result.invalid_geometries:
        raise RuntimeError(
            f"{layer.table_name}: "
            f"{result.invalid_geometries} invalid geometries detected."
        )

    return result


# =============================================================================
# Metadata update
# =============================================================================


def update_metadata(
    con: duckdb.DuckDBPyConnection,
    layer: BoundaryLayer,
    zip_path: Path,
    validation: ValidationResult,
) -> None:

    file_hash = sha256_file(
        zip_path
    )

    downloaded_at = utc_from_timestamp(
        zip_path.stat().st_mtime
    )

    loaded_at = (
        datetime
        .now(timezone.utc)
        .replace(tzinfo=None)
    )

    con.execute(
        """
        INSERT INTO abs_boundary_sources (
            geography_type,
            table_name,
            description,
            reference_year,
            asgs_edition,
            datum,
            epsg,
            source_url,
            source_file,
            source_sha256,
            downloaded_at_utc,
            loaded_at_utc,
            row_count,
            non_spatial_count
        )

        VALUES (
            ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?
        )

        ON CONFLICT (
            geography_type,
            reference_year
        )

        DO UPDATE SET
            table_name = excluded.table_name,
            description = excluded.description,
            asgs_edition = excluded.asgs_edition,
            datum = excluded.datum,
            epsg = excluded.epsg,
            source_url = excluded.source_url,
            source_file = excluded.source_file,
            source_sha256 = excluded.source_sha256,
            downloaded_at_utc = excluded.downloaded_at_utc,
            loaded_at_utc = excluded.loaded_at_utc,
            row_count = excluded.row_count,
            non_spatial_count = excluded.non_spatial_count
        """,
        [
            layer.key.upper(),
            layer.table_name,
            layer.description,
            layer.reference_year,
            layer.asgs_edition,
            layer.datum,
            layer.epsg,
            layer.url,
            zip_path.name,
            file_hash,
            downloaded_at,
            loaded_at,
            validation.row_count,
            validation.null_geometries,
        ],
    )


# =============================================================================
# Views
# =============================================================================


def create_views(
    con: duckdb.DuckDBPyConnection,
) -> None:

    for layer in LAYERS.values():

        if not table_exists(
            con,
            layer.table_name,
        ):
            continue

        view_name = quote_identifier(
            f"{layer.table_name}_meta"
        )

        table_name = quote_identifier(
            layer.table_name
        )

        con.execute(
            f"""
            CREATE OR REPLACE VIEW {view_name} AS

            SELECT * EXCLUDE (geom)

            FROM {table_name}
            """
        )

    if (
        table_exists(con, "abs_sa2")
        and table_exists(con, "abs_lga")
        and table_exists(con, "abs_ucl")
    ):

        con.execute(
            """
            CREATE OR REPLACE VIEW abs_geographies AS

            SELECT
                'SA2'::VARCHAR AS geography_type,
                sa2_code AS geography_code,
                sa2_name AS geography_name,
                state_code,
                state_name,
                area_sqkm,
                reference_year,
                asgs_edition,
                datum,
                epsg,
                geom

            FROM abs_sa2

            UNION ALL

            SELECT
                'LGA',
                lga_code,
                lga_name,
                state_code,
                state_name,
                area_sqkm,
                reference_year,
                asgs_edition,
                datum,
                epsg,
                geom

            FROM abs_lga

            UNION ALL

            SELECT
                'UCL',
                ucl_code,
                ucl_name,
                state_code,
                state_name,
                area_sqkm,
                reference_year,
                asgs_edition,
                datum,
                epsg,
                geom

            FROM abs_ucl
            """
        )


# =============================================================================
# Process one layer
# =============================================================================


def process_layer(
    con: duckdb.DuckDBPyConnection,
    layer: BoundaryLayer,
    cache_dir: Path,
    force_download: bool,
    reload: bool,
) -> None:

    LOG.info("")
    LOG.info("=" * 72)

    LOG.info(
        "%s %s | ASGS Edition %s",
        layer.key.upper(),
        layer.reference_year,
        layer.asgs_edition,
    )

    LOG.info("=" * 72)

    layer_cache = (
        cache_dir
        / layer.key
        / str(layer.reference_year)
    )

    zip_path = (
        layer_cache
        / Path(layer.url).name
    )

    extraction_dir = (
        layer_cache
        / "extracted"
    )

    zip_path = download_file(
        layer.url,
        zip_path,
        force=force_download,
    )

    shp_path = extract_boundary(
        zip_path,
        extraction_dir,
        layer.expected_shapefile,
        force=force_download,
    )

    changed = load_layer(
        con,
        layer,
        shp_path,
        reload=reload,
    )

    validation = validate_loaded_layer(
        con,
        layer,
    )

    update_metadata(
        con,
        layer,
        zip_path,
        validation,
    )

    # Use f-string formatting here because Python logging's old-style
    # %-formatting does not support the comma thousands separator.
    LOG.info(
        f"{layer.key.upper()}: "
        f"{validation.row_count:,} rows | "
        f"{validation.distinct_codes:,} distinct codes"
    )

    LOG.info(
        "Geometry validation: "
        "%d non-spatial | %d empty | %d invalid",
        validation.null_geometries,
        validation.empty_geometries,
        validation.invalid_geometries,
    )

    if changed:
        LOG.info(
            "Loaded/rebuilt table: %s",
            layer.table_name,
        )
    else:
        LOG.info(
            "Existing table retained: %s",
            layer.table_name,
        )


# =============================================================================
# Summary
# =============================================================================


def print_summary(
    con: duckdb.DuckDBPyConnection,
) -> None:

    print()
    print("=" * 100)
    print("ABS boundary database")
    print("=" * 100)

    rows = con.execute(
        """
        SELECT
            geography_type,
            reference_year,
            asgs_edition,
            row_count,
            non_spatial_count,
            datum,
            epsg,
            table_name

        FROM abs_boundary_sources

        ORDER BY geography_type
        """
    ).fetchall()

    if not rows:
        print("No layers loaded.")
        return

    print(
        f"{'TYPE':<6}"
        f"{'YEAR':>7}"
        f"{'ED':>5}"
        f"{'ROWS':>10}"
        f"{'NON-SPATIAL':>14}  "
        f"{'DATUM':<10}"
        f"{'EPSG':>6}  "
        f"TABLE"
    )

    print("-" * 100)

    for (
        geography_type,
        year,
        edition,
        count,
        non_spatial_count,
        datum,
        epsg,
        table_name,
    ) in rows:

        print(
            f"{geography_type:<6}"
            f"{year:>7}"
            f"{edition:>5}"
            f"{count:>10,}"
            f"{non_spatial_count:>14,}  "
            f"{datum:<10}"
            f"{epsg:>6}  "
            f"{table_name}"
        )

    print()


# =============================================================================
# Layer selection
# =============================================================================


def selected_layers(
    keys: Iterable[str],
) -> list[BoundaryLayer]:

    return [
        LAYERS[key]
        for key in keys
    ]


# =============================================================================
# Command line
# =============================================================================


def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Download, cache and load ABS SA2, LGA and UCL "
            "boundaries into DuckDB Spatial."
        ),
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=PROJECT_DIR / "abs_boundaries.duckdb",
        help=(
            "DuckDB database file "
            "(default: ./abs_boundaries.duckdb)"
        ),
    )

    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=PROJECT_DIR / "cache",
        help=(
            "Boundary download/extraction cache "
            "(default: ./cache)"
        ),
    )

    parser.add_argument(
        "--layers",
        nargs="+",
        choices=sorted(LAYERS),
        default=list(LAYERS),
        help=(
            "Layers to process "
            "(default: sa2 lga ucl)"
        ),
    )

    parser.add_argument(
        "--force-download",
        action="store_true",
        help=(
            "Download ABS ZIP files again even if cached."
        ),
    )

    parser.add_argument(
        "--reload",
        action="store_true",
        help=(
            "Rebuild DuckDB boundary tables even if they already exist."
        ),
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )

    return parser.parse_args()


# =============================================================================
# Main
# =============================================================================


def main() -> None:

    args = parse_args()

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.verbose
            else logging.INFO
        ),
        format=(
            "%(asctime)s | "
            "%(levelname)-8s | "
            "%(message)s"
        ),
    )

    LOG.info(
        "DuckDB database: %s",
        args.db.resolve(),
    )

    LOG.info(
        "ABS cache: %s",
        args.cache_dir.resolve(),
    )

    LOG.info(
        "Layers: %s",
        ", ".join(args.layers),
    )

    con = connect_database(
        args.db
    )

    try:
        ensure_metadata_table(
            con
        )

        for layer in selected_layers(
            args.layers
        ):
            process_layer(
                con=con,
                layer=layer,
                cache_dir=args.cache_dir,
                force_download=args.force_download,
                reload=args.reload,
            )

        create_views(
            con
        )

        print_summary(
            con
        )

    finally:
        con.close()

    LOG.info("Done.")


if __name__ == "__main__":
    main()