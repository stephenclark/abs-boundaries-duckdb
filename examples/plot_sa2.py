#!/usr/bin/env python3
"""
plot_sa2.py

Query ABS SA2 boundaries from DuckDB and create:

    sa2_australia.png   high-resolution raster image
    sa2_australia.svg   vector image

The SVG is preferable when zooming into boundary detail.
"""

from pathlib import Path

import duckdb
import geopandas as gpd
import matplotlib.pyplot as plt


PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "abs_boundaries.duckdb"

PNG_PATH = PROJECT_DIR / "sa2_australia.png"
SVG_PATH = PROJECT_DIR / "sa2_australia.svg"


def main() -> None:
    con = duckdb.connect(str(DB_PATH))

    try:
        con.execute("LOAD spatial")

        df = con.execute(
            """
            SELECT
                sa2_code,
                sa2_name,
                state_code,
                state_name,
                area_sqkm,
                ST_AsWKB(geom) AS geometry
            FROM abs_sa2
            WHERE geom IS NOT NULL
            """
        ).fetchdf()

    finally:
        con.close()

    geometry = gpd.GeoSeries.from_wkb(
        df["geometry"].map(bytes),
        crs="EPSG:7844",
    )

    gdf = gpd.GeoDataFrame(
        df.drop(columns="geometry"),
        geometry=geometry,
    )

    print(f"Loaded {len(gdf):,} spatial SA2 regions.")
    print(f"CRS: {gdf.crs}")
    print(f"Bounds: {gdf.total_bounds}")

    fig, ax = plt.subplots(
        figsize=(20, 18)
    )

    gdf.plot(
        ax=ax,
        facecolor="white",
        edgecolor="black",
        linewidth=0.20,
    )

    ax.set_title(
        "ABS Statistical Area Level 2 (SA2) Boundaries, 2026",
        fontsize=18,
    )

    ax.set_axis_off()

    plt.tight_layout()

    # High-resolution raster output.
    fig.savefig(
        PNG_PATH,
        dpi=600,
        bbox_inches="tight",
    )

    # Vector output: remains sharp when zoomed.
    fig.savefig(
        SVG_PATH,
        format="svg",
        bbox_inches="tight",
    )

    print(f"Saved PNG: {PNG_PATH}")
    print(f"Saved SVG: {SVG_PATH}")

    plt.show()


if __name__ == "__main__":
    main()