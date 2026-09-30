#!/usr/bin/env python3
"""
plot_ucl.py

Query ABS Urban Centre and Locality boundaries from DuckDB and create:

    ucl_australia.png   high-resolution raster image
    ucl_australia.svg   vector image
"""

from pathlib import Path

import duckdb
import geopandas as gpd
import matplotlib.pyplot as plt


PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "abs_boundaries.duckdb"

PNG_PATH = PROJECT_DIR / "ucl_australia.png"
SVG_PATH = PROJECT_DIR / "ucl_australia.svg"


def main() -> None:
    con = duckdb.connect(str(DB_PATH))

    try:
        con.execute("LOAD spatial")

        df = con.execute(
            """
            SELECT
                ucl_code,
                ucl_name,
                state_code,
                state_name,
                area_sqkm,
                ST_AsWKB(geom) AS geometry
            FROM abs_ucl
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

    print(f"Loaded {len(gdf):,} spatial UCL regions.")
    print(f"CRS: {gdf.crs}")
    print(f"Bounds: {gdf.total_bounds}")

    fig, ax = plt.subplots(
        figsize=(20, 18)
    )

    gdf.plot(
        ax=ax,
        facecolor="white",
        edgecolor="black",
        linewidth=0.25,
    )

    ax.set_title(
        "ABS Urban Centres and Localities (UCL), 2021",
        fontsize=18,
    )

    ax.set_axis_off()

    plt.tight_layout()

    fig.savefig(
        PNG_PATH,
        dpi=600,
        bbox_inches="tight",
    )

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