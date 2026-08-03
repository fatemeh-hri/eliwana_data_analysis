import pandas as pd
import geopandas as gpd
from shapely import wkt
import snowflake.connector
from sqlalchemy import create_engine
import ezdxf
import math
import traceback
from datetime import datetime
from functools import lru_cache
import networkx as nx
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union, nearest_points
from shapely.affinity import translate
from lxml import etree

import json

from dash import Dash, dcc, html
from dash.dependencies import Input, Output
import dash_leaflet as dl

try:
    from centerline.geometry import Centerline
except ImportError:
    Centerline = None


# -----------------------------
# CONFIG
# -----------------------------
DXF_PATH = r"Map_Lane/FMS_Map_20260520_135053.dxf"
KML_FILE = r"Map_Layer/ELI_Area.kml"
GPKG_PATH = r"Map_Lane/FMS_Map_20260520_135053.gpkg"
WMS_URL = "https://fortescuesky.fmgl.com.au/SG/default/streamer.ashx?"

SOURCE_CRS = "EPSG:28350"
TARGET_CRS = "EPSG:4326"

# DEFAULT_START_DATETIME = "2026-05-09 11:19:01"
# DEFAULT_END_DATETIME = "2026-05-09 13:19:01"

# DEFAULT_START_DATETIME = "2026-06-15 11:00:00"
# DEFAULT_END_DATETIME = "2026-06-15 11:30:00"

DEFAULT_START_DATETIME = "2026-07-01 11:00:00"
DEFAULT_END_DATETIME = "2026-07-01 11:30:00"


HOV_ASSET_TYPES = ["DZ", "WD", "WC", "GR", "LV", "ELI", "WL", "EX"]

# Colour coding for trajectory points by operational area type.
AREA_COLORS = {
    "haul road": "#1f77b4",       # Blue
    "intersection": "#ff8c00",    # Orange
    "dynamic area": "#2ca02c",    # Green
}
DEFAULT_AREA_COLOR = "#808080"     # Grey for unclassified points

# Intersection detection approach:
#   1. buffer lane LineStrings to create road-surface polygons,
#   2. generate road centrelines from those polygons,
#   3. build a graph from centreline segments,
#   4. detect compact clusters of graph nodes with degree >= 3,
#   5. create polygons around the local centreline branches.
# This avoids false intersections caused by many parallel lane self-join rows.

# LANE_BUFFER_METRES = 10
LANE_BUFFER_METRES = 12
INTERSECTION_POINT_TOLERANCE_METRES = 10

ROAD_POLYGON_BUFFER_METRES = 8
CENTERLINE_INTERPOLATION_DISTANCE = 5
NODE_SNAP_METRES = 5
# NODE_SNAP_METRES = 8
MIN_GRAPH_DEGREE = 3
INTERSECTION_CLUSTER_BUFFER_METRES = 30
# INTERSECTION_CLUSTER_BUFFER_METRES = 35
INTERSECTION_POLYGON_RADIUS_METRES = 45
INTERSECTION_POLYGON_BUFFER_METRES = 12
MIN_BRANCH_COUNT = 3
MIN_DIRECTION_GROUPS = 3
# MIN_DIRECTION_GROUPS = 2

DEBUG = True


# -----------------------------
# DEBUGGING
# -----------------------------
def debug_log(label, value=None):
    if not DEBUG:
        return

    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] {label}", flush=True)

    if value is not None:
        if isinstance(value, pd.DataFrame):
            print(f"shape={value.shape}", flush=True)
            print(value.head(), flush=True)
        else:
            print(value, flush=True)

# Define intersection boxes in SOURCE_CRS metres: (name, min_x, min_y, max_x, max_y).
# Add/edit boxes after visually checking the X markers against the map.
INTERSECTION_BOXES = [
    # ("Intersection 1", 650000, 7550000, 650100, 7550100),
]
INTERSECTION_POLYGONS = [
    (
        "Intersection 1",
        Polygon([
            (482120.50207447, 7513691.76620729),
            (482045.53212524, 7513702.21283956),
            (482048.60466414, 7513848.46569136),
            (482037.54352409, 7513865.05740143),
            (482072.57046758, 7513920.97760947),
            (482214.52176491, 7513850.92372248),
            (482193.62850037, 7513686.85014505),
            (482120.50207447, 7513691.76620729),
        ]),
        
    )
]

# -----------------------------
# MANUAL INTERSECTION EDITS
# -----------------------------
# Use this section to correct automatically detected intersection polygons.
# All offsets are in SOURCE_CRS metres, so:
#   x_offset_m < 0 = move west/left
#   x_offset_m > 0 = move east/right
#   y_offset_m < 0 = move south/down
#   y_offset_m > 0 = move north/up
#
# Example from your screenshot:
#   - remove Centerline Intersection 56, 162, and 372
#   - move Centerline Intersection 70 and 161 slightly down-left
#
# You can add more names here after checking the map labels.
MANUAL_DELETE_INTERSECTIONS = {
    "Centerline Intersection 56",
    "Centerline Intersection 162",
    "Centerline Intersection 372",
    "Centerline Intersection 261",
    "Centerline Intersection 369",
    "Centerline Intersection 361",
    "Centerline Intersection 387",
    "Centerline Intersection 318",
    "Centerline Intersection 377",
    "Centerline Intersection 117",
    "Centerline Intersection 50",
    "Centerline Intersection 364",
    "Centerline Intersection 198",
    "Centerline Intersection 165",
    "Centerline Intersection 4",
    "Centerline Intersection 348",
    "Centerline Intersection 311",
    "Centerline Intersection 159",
    "Centerline Intersection 88",
    "Centerline Intersection 320",
    "Centerline Intersection 125",
    "Centerline Intersection 373",
    "Centerline Intersection 15",
    "Centerline Intersection 384",
    "Centerline Intersection 319",
    "Centerline Intersection 101",
    "Centerline Intersection 323",
    "Centerline Intersection 322",
    "Centerline Intersection 226",
    "Centerline Intersection 294",
    "Centerline Intersection 138",
    "Centerline Intersection 388",
    "Centerline Intersection 290",
    "Centerline Intersection 308",
    "Centerline Intersection 97",
    "Centerline Intersection 362",
    "Centerline Intersection 157",
    "Centerline Intersection 24",
    "Centerline Intersection 386",
    "Centerline Intersection 237",
    "Centerline Intersection 254",
    "Centerline Intersection 378",
    "Centerline Intersection 49",
    "Centerline Intersection 297",
    "Centerline Intersection 356",
    "Centerline Intersection 74",
    "Centerline Intersection 349",
    "Centerline Intersection 355",
    "Centerline Intersection 279",
    "Centerline Intersection 295",
    "Centerline Intersection 288",
    "Centerline Intersection 371",
    "Centerline Intersection 266",
    "Centerline Intersection 128",
    "Centerline Intersection 240",
    "Centerline Intersection 370",
    "Centerline Intersection 291",
    "Centerline Intersection 337",
    "Centerline Intersection 260",
    "Centerline Intersection 363",
    "Centerline Intersection 306",
    "Centerline Intersection 276",
    "Centerline Intersection 352",
    "Centerline Intersection 140",
    "Centerline Intersection 289",
    "Centerline Intersection 228",
    "Centerline Intersection 95",
    "Centerline Intersection 277",
    "Centerline Intersection 137",
    "Centerline Intersection 136",
    "Centerline Intersection 324",
    "Centerline Intersection 258",
    "Centerline Intersection 119",
    "Centerline Intersection 132",
    "Centerline Intersection 16",
    "Centerline Intersection 66",
    "Centerline Intersection 139",
    "Centerline Intersection 28",
    "Centerline Intersection 229",
    "Centerline Intersection 123",
    "Centerline Intersection 133",
    "Centerline Intersection 292",
    "Centerline Intersection 2",
    "Centerline Intersection 328",
    "Centerline Intersection 251",
    "Centerline Intersection 264",
    "Centerline Intersection 127",
    "Centerline Intersection 124",
    "Centerline Intersection 293",
    "Centerline Intersection 310",
    "Centerline Intersection 121",
    "Centerline Intersection 205",
    "Centerline Intersection 23",
    "Centerline Intersection 262",
    "Centerline Intersection 71",
    "Centerline Intersection 3",
    "Centerline Intersection 122",
    "Centerline Intersection 304",
    "Centerline Intersection 232",
    "Centerline Intersection 154",
    "Centerline Intersection 309",
    "Centerline Intersection 231",
    "Centerline Intersection 389",
    "Centerline Intersection 244",
    "Centerline Intersection 179",
    "Centerline Intersection 332",
    "Centerline Intersection 259",
    "Centerline Intersection 330",
    "Centerline Intersection 333",
    "Centerline Intersection 338",
    "Centerline Intersection 343",
    "Centerline Intersection 177",
    "Centerline Intersection 267",
    "Centerline Intersection 112",
    "Centerline Intersection 134",
    "Centerline Intersection 298",
    "Centerline Intersection 350",
    "Centerline Intersection 255",
    "Centerline Intersection 314",
    "Centerline Intersection 45",
    "Centerline Intersection 82",
    "Centerline Intersection 313",
    "Centerline Intersection 357",
    "Centerline Intersection 256",
    "Centerline Intersection 169",
    "Centerline Intersection 72",
    "Centerline Intersection 147",
    "Centerline Intersection 152",
    "Centerline Intersection 347",
    "Centerline Intersection 336",
    "Centerline Intersection 217",
    "Centerline Intersection 367",
    "Centerline Intersection 280",
    "Centerline Intersection 73",
    "Centerline Intersection 46",
    "Centerline Intersection 61",
    "Centerline Intersection 43",
    "Centerline Intersection 346",
    "Centerline Intersection 35",
    "Centerline Intersection 38",
    "Centerline Intersection 39",
    "Centerline Intersection 144",
    "Centerline Intersection 149",
    "Centerline Intersection 211",
    "Centerline Intersection 41",
    "Centerline Intersection 151",
    "Centerline Intersection 37",
    "Centerline Intersection 40",
    "Centerline Intersection 42",
    "Centerline Intersection 145",
    "Centerline Intersection 374",
    "Centerline Intersection 334",
    "Centerline Intersection 331",
    "Centerline Intersection 153",
    "Centerline Intersection 385",
    "Centerline Intersection 69",
    "Centerline Intersection 146",
    "Centerline Intersection 247",
    "Centerline Intersection 234",
    "Centerline Intersection 214",
    "Centerline Intersection 235",
    "Centerline Intersection 246",
    "Centerline Intersection 111",
    "Centerline Intersection 43",
    "Centerline Intersection 265",
    "Centerline Intersection 253",
    "Centerline Intersection 296",
    "Centerline Intersection 354",
    "Centerline Intersection 312",
    "Centerline Intersection 329",
    "Centerline Intersection 344",
    "Centerline Intersection 365",
    "Centerline Intersection 163",
    "Centerline Intersection 358",
    "Centerline Intersection 52",
    "Centerline Intersection 65",
    "Centerline Intersection 68",
    "Centerline Intersection 148",
    "Centerline Intersection 54",
    "Centerline Intersection 381",
    "Centerline Intersection 143",
    "Centerline Intersection 257",
    "Centerline Intersection 300",
    "Centerline Intersection 284",
    "Centerline Intersection 171",
    "Centerline Intersection 170",
    "Centerline Intersection 194",
    "Centerline Intersection 7",
    "Centerline Intersection 84",
    "Centerline Intersection 62",
    "Centerline Intersection 250",
    "Centerline Intersection 172",
    "Centerline Intersection 273",
    "Centerline Intersection 248",
    "Centerline Intersection 212",
    "Centerline Intersection 193",
    "Centerline Intersection 285",
    "Centerline Intersection 221",
    "Centerline Intersection 382",
    "Centerline Intersection 316",
    "Centerline Intersection 202",
    "Centerline Intersection 383",
    "Centerline Intersection 130",
    "Centerline Intersection 287",
    "Centerline Intersection 20",
    "Centerline Intersection 302",
    "Centerline Intersection 286",
    "Centerline Intersection 317",
    "Centerline Intersection 274",
    "Centerline Intersection 53",
    "Centerline Intersection 325",
    "Centerline Intersection 303",
    "Centerline Intersection 376",
    "Centerline Intersection 390",
    "Centerline Intersection 335",
    "Centerline Intersection 185",
    "Centerline Intersection 142",
    "Centerline Intersection 305",
    "Centerline Intersection 281",
    "Centerline Intersection 282",
    "Centerline Intersection 63",
    "Centerline Intersection 299",
    "Centerline Intersection 368",
    "Centerline Intersection 375",
    "Centerline Intersection 223",
    "Centerline Intersection 321",
    "Centerline Intersection 380",
    "Centerline Intersection 360",
    "Centerline Intersection 307",
    "Centerline Intersection 110",
    "Intersection 1",
    "Centerline Intersection 64",
    "Centerline Intersection 60",
    "Centerline Intersection 160",
    "Centerline Intersection 327",
    "Centerline Intersection 190",
    "Centerline Intersection 366",
    "Centerline Intersection 379",
    "Centerline Intersection 85",
    "Centerline Intersection 252",
    "Centerline Intersection 141",
    "Centerline Intersection 34",
    "Centerline Intersection 315",
    "Centerline Intersection 118",
    "Centerline Intersection 51",
    "Centerline Intersection 271",
    "Centerline Intersection 391",
    "Centerline Intersection 249",
    "Centerline Intersection 126",
    "Centerline Intersection 92",
    "Centerline Intersection 239",
    "Centerline Intersection 359",
    "Centerline Intersection 351",
    "Centerline Intersection 268", # It doesn't seem to be an intersection
    "Centerline Intersection 36", # It doesn't seem to be an intersection
    "Centerline Intersection 197", # It doesn't seem to be an intersection
    "Centerline Intersection 269", # It doesn't seem to be an intersection
    "Centerline Intersection 135", # It doesn't seem to be an intersection


}

MANUAL_MOVE_INTERSECTIONS = {
    # name: (x_offset_m, y_offset_m)
    "Centerline Intersection 70": (5, -45),
    "Centerline Intersection 161": (-35, 25),
    "Centerline Intersection 75": (50, 25),
    "Centerline Intersection 32": (5, -10),
    "Centerline Intersection 107": (-100, -80),
    "Centerline Intersection 278": (30, 0), # should change the polygon shape, but moving it is a temporary fix
    "Centerline Intersection 242": (-30, 0),
    "Centerline Intersection 131": (40, 0),
    "Centerline Intersection 197": (-60, 40),
    "Centerline Intersection 1": (10, 40),
    "Centerline Intersection 86": (20, -30),
    "Centerline Intersection 77": (-20, 10),
    "Centerline Intersection 78": (-20, -70),
    "Centerline Intersection 155": (10, -60),
    "Centerline Intersection 168": (-20, 60),
    "Centerline Intersection 30": (30, 50),
    "Centerline Intersection 150": (880, -170),
    "Centerline Intersection 208": (170, 0),
    "Centerline Intersection 326": (80, -110),
    "Centerline Intersection 301": (-110, 20),
    "Centerline Intersection 96": (-30, -10),
    "Centerline Intersection 114": (-70, -10),
    "Centerline Intersection 283": (-170, 80),
    "Centerline Intersection 14": (5, -25),
    "Centerline Intersection 272": (250, 200),
    "Centerline Intersection 81": (-50, 20),
    "Centerline Intersection 11": (100, -50),
    "Centerline Intersection 27": (-700, -600),
    "Centerline Intersection 220": (100, 10),
    "Centerline Intersection 340": (300, -350),
    "Centerline Intersection 275": (645, 340),
    "Centerline Intersection 90": (-70, 10),
    "Centerline Intersection 109": (-20, -10), 
    "Centerline Intersection 222": (0, 50), 
    "Centerline Intersection 58": (-40, 20), 
    "Centerline Intersection 44": (-20, -30), 
    "Centerline Intersection 89": (10, 10), 
    "Centerline Intersection 48": (20, -10), 
    "Centerline Intersection 263": (-30, 0), 
    "Centerline Intersection 47": (30, -10), 
    "Centerline Intersection 6": (0, 60), 
    "Centerline Intersection 8": (-90, 0), 
    "Centerline Intersection 216": (30, -10), 
    "Centerline Intersection 156": (-20, -30), 
    "Centerline Intersection 83": (0, 20), 
    "Centerline Intersection 219": (0, -10), 
    "Centerline Intersection 218": (0, -10),
    "Centerline Intersection 5": (20, -30),
}

# -----------------------------
# SWAP INTERSECTIONS
# -----------------------------
# Swap the geometry of two detected intersections.
MANUAL_SWAP_INTERSECTIONS = [
    # (
    #     "Centerline Intersection 70",
    #     "Centerline Intersection 161",
    # ),
]

# -----------------------------
# COPY INTERSECTIONS
# -----------------------------
# Copy an existing polygon and create a new intersection.
#
# offset_xy is in SOURCE_CRS metres.
MANUAL_COPY_INTERSECTIONS = [
    {
        "SOURCE": "Centerline Intersection 241",
        "NEW_NAME": "Centerline Intersection 500",
        "OFFSET_XY": (380, -80),
    },
    {
        "SOURCE": "Centerline Intersection 242",
        "NEW_NAME": "Centerline Intersection 501",
        "OFFSET_XY": (100, -20),
    },
    {
        "SOURCE": "Centerline Intersection 213",
        "NEW_NAME": "Centerline Intersection 502",
        "OFFSET_XY": (760, 150),
    },
    {
        "SOURCE": "Centerline Intersection 96",
        "NEW_NAME": "Centerline Intersection 503",
        "OFFSET_XY": (100, 20),
    },
    
    {
        "SOURCE": "Centerline Intersection 96",
        "NEW_NAME": "Centerline Intersection 504",
        "OFFSET_XY": (180, 34),
    },
    {
        "SOURCE": "Centerline Intersection 26",
        "NEW_NAME": "Centerline Intersection 505",
        "OFFSET_XY": (605, -250),
    },
    {
        "SOURCE": "Centerline Intersection 26",
        "NEW_NAME": "Centerline Intersection 506",
        "OFFSET_XY": (530, -220),
    },
    {
        "SOURCE": "Centerline Intersection 114",
        "NEW_NAME": "Centerline Intersection 507",
        "OFFSET_XY": (140, 0),
    },
    {
        "SOURCE": "Centerline Intersection 283",
        "NEW_NAME": "Centerline Intersection 508",
        "OFFSET_XY": (-160, 100),
    },
    {
        "SOURCE": "Centerline Intersection 14",
        "NEW_NAME": "Centerline Intersection 509",
        "OFFSET_XY": (-10, 270),
    },
    {
        "SOURCE": "Centerline Intersection 76",
        "NEW_NAME": "Centerline Intersection 510",
        "OFFSET_XY": (-10, -100),
    },
    {
        "SOURCE": "Centerline Intersection 90",
        "NEW_NAME": "Centerline Intersection 511",
        "OFFSET_XY": (50, -150),
    },
    {
        "SOURCE": "Centerline Intersection 220",
        "NEW_NAME": "Centerline Intersection 512",
        "OFFSET_XY": (150, 0),
    },
    {
        "SOURCE": "Centerline Intersection 220",
        "NEW_NAME": "Centerline Intersection 513",
        "OFFSET_XY": (310, 0),
    },
    {
        "SOURCE": "Centerline Intersection 90",
        "NEW_NAME": "Centerline Intersection 514",
        "OFFSET_XY": (100, 0),
    },
    {
        "SOURCE": "Centerline Intersection 58",
        "NEW_NAME": "Centerline Intersection 515",
        "OFFSET_XY": (95, -80),
    },
    {
        "SOURCE": "Centerline Intersection 44",
        "NEW_NAME": "Centerline Intersection 516",
        "OFFSET_XY": (80, 100),
    },
    {
        "SOURCE": "Centerline Intersection 99",
        "NEW_NAME": "Centerline Intersection 517",
        "OFFSET_XY": (120, 180),
    },
    {
        "SOURCE": "Centerline Intersection 180",
        "NEW_NAME": "Centerline Intersection 518",
        "OFFSET_XY": (-150, 40),
    },
    {
        "SOURCE": "Centerline Intersection 99",
        "NEW_NAME": "Centerline Intersection 519",
        "OFFSET_XY": (480, -150),
    },
    {
        "SOURCE": "Centerline Intersection 208",
        "NEW_NAME": "Centerline Intersection 520",
        "OFFSET_XY": (400, -620),
    },
    {
        "SOURCE": "Centerline Intersection 150",
        "NEW_NAME": "Centerline Intersection 521",
        "OFFSET_XY": (-600, -400),
    },
    {
        "SOURCE": "Centerline Intersection 156",
        "NEW_NAME": "Centerline Intersection 522",
        "OFFSET_XY": (120, 0),
    },
    {
        "SOURCE": "Centerline Intersection 158",
        "NEW_NAME": "Centerline Intersection 523",
        "OFFSET_XY": (610, -130),
    },
    {
        "SOURCE": "Centerline Intersection 206",
        "NEW_NAME": "Centerline Intersection 524",
        "OFFSET_XY": (0, 95),
    },
]

# Optional: replace a detected polygon completely with your own manually drawn polygon.
# Coordinates must be SOURCE_CRS metres.
# This is useful when moving is not enough and the shape itself is wrong.
MANUAL_REPLACE_INTERSECTION_POLYGONS = {
    # "Centerline Intersection 70": Polygon([
    #     (482000, 7513000),
    #     (482050, 7513000),
    #     (482050, 7513050),
    #     (482000, 7513050),
    #     (482000, 7513000),
    # ]),
}

# Optional: add completely new intersections that were missed by the detector.
# Coordinates must be SOURCE_CRS metres.
MANUAL_ADD_INTERSECTION_POLYGONS = [
    # (
    #     "Manual Intersection A",
    #     Polygon([
    #         (482000, 7513000),
    #         (482050, 7513000),
    #         (482050, 7513050),
    #         (482000, 7513050),
    #         (482000, 7513000),
    #     ]),
    # ),
]

PRINT_INTERSECTIONS_POLYGONS = {
    "Centerline Intersection 242",
}
# -----------------------------
# HELPER FUNCTION
# -----------------------------
def clean_datetime(value, default_value):
    if value is None or str(value).strip() == "":
        return default_value

    return str(value).strip()


# -----------------------------
# DXF LOADER
# -----------------------------
def load_dxf_lanes(dxf_path):
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    records = []

    for e in msp.query("LWPOLYLINE"):
        layer_name = e.dxf.layer.upper()

        if "LANES" not in layer_name:
            continue

        pts = [(p[0], p[1]) for p in e.get_points()]

        if len(pts) < 2:
            continue

        records.append({
            "polyline_id": e.dxf.handle,
            "layer": layer_name,
            "vertex_count": len(pts),
            "geometry": LineString(pts)
        })

    dxf_gdf = gpd.GeoDataFrame(
        records,
        geometry="geometry",
        crs=SOURCE_CRS
    )

    dxf_gdf = dxf_gdf.to_crs(TARGET_CRS)

    dxf_gdf["geometry"] = dxf_gdf.geometry.simplify(
        0.000003,
        preserve_topology=True
    )

    return dxf_gdf

# -----------------------------
# DETECT INTERSECTIONS (HYBRID)
# -----------------------------
def detect_intersections_hybrid(lane_gdf):
    # centerline_outputs = detect_intersections_with_centerline_package(DXF_PATH)
    # centerline_intersections = centerline_outputs["intersections"]
    centerline_intersections = detect_intersections_from_dxf_lanes(lane_gdf)
    fallback_intersections = empty_intersection_gdf()

    combined = pd.concat(
        [centerline_intersections, fallback_intersections],
        ignore_index=True,
    )

    if combined.empty:
        return centerline_intersections

    combined = gpd.GeoDataFrame(
        combined,
        geometry="geometry",
        crs=TARGET_CRS,
    )

    combined_source = combined.to_crs(SOURCE_CRS).copy()
    combined_source["CENTROID"] = combined_source.geometry.centroid

    final_records = []

    used = set()

    for idx, row in combined_source.iterrows():
        if idx in used:
            continue

        nearby_idx = combined_source[
            combined_source["CENTROID"].distance(row["CENTROID"]) <= 40
        ].index.tolist()

        used.update(nearby_idx)

        nearby = combined_source.loc[nearby_idx]

        merged_polygon = nearby.geometry.union_all().convex_hull.buffer(0)

        final_records.append({
            "INTERSECTION_NAME": f"Hybrid Intersection {len(final_records) + 1}",
            "DETECTION_METHOD": ",".join(
                sorted(set(nearby.get("DETECTION_METHOD", pd.Series(["unknown"])).fillna("unknown")))
            ),
            "INTERSECTION_X": merged_polygon.centroid.x,
            "INTERSECTION_Y": merged_polygon.centroid.y,
            "geometry": merged_polygon,
        })

    result = gpd.GeoDataFrame(
        final_records,
        geometry="geometry",
        crs=SOURCE_CRS,
    ).to_crs(TARGET_CRS)
    result["DETECTION_METHOD"] = "LANE_SELF_JOIN_FALLBACK"
    return result

# -----------------------------
# CENTERLINE / GRAPH INTERSECTION DETECTOR
# -----------------------------
def empty_intersection_gdf(crs=TARGET_CRS):
    return gpd.GeoDataFrame(
        {
            "INTERSECTION_NAME": [],
            "INTERSECTION_X": [],
            "INTERSECTION_Y": [],
            "GRAPH_NODE_COUNT": [],
            "MAX_GRAPH_DEGREE": [],
            "BRANCH_COUNT": [],
            "DIRECTION_GROUP_COUNT": [],
        },
        geometry=[],
        crs=crs,
    )


def _explode_lines(gdf):
    rows = []

    for _, row in gdf.iterrows():
        geom = row.geometry

        if geom is None or geom.is_empty:
            continue

        if geom.geom_type == "LineString":
            rows.append(row.copy())

        elif geom.geom_type == "MultiLineString":
            for part in geom.geoms:
                if part is None or part.is_empty:
                    continue

                new_row = row.copy()
                new_row.geometry = part
                rows.append(new_row)

    if not rows:
        return gpd.GeoDataFrame(geometry=[], crs=gdf.crs)

    return gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs)


def _snap_value(value, snap_m=NODE_SNAP_METRES):
    return round(value / snap_m) * snap_m


def _snap_point_key(point, snap_m=NODE_SNAP_METRES):
    return (_snap_value(point.x, snap_m), _snap_value(point.y, snap_m))


def _line_direction_bin(line, bin_degrees=30):
    coords = list(line.coords)

    if len(coords) < 2:
        return None

    x1, y1 = coords[0]
    x2, y2 = coords[-1]
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180

    return int(angle // bin_degrees)


def _create_road_polygons_from_lanes(lane_gdf, buffer_m=ROAD_POLYGON_BUFFER_METRES):
    if lane_gdf.empty:
        return gpd.GeoDataFrame(
            {"ROAD_POLYGON_ID": []},
            geometry=[],
            crs=SOURCE_CRS,
        )

    lanes_source = lane_gdf.to_crs(SOURCE_CRS).copy()
    lanes_source = lanes_source[
        lanes_source.geometry.notna()
        & ~lanes_source.geometry.is_empty
        & lanes_source.geometry.type.isin(["LineString", "MultiLineString"])
    ].copy()

    if lanes_source.empty:
        return gpd.GeoDataFrame(
            {"ROAD_POLYGON_ID": []},
            geometry=[],
            crs=SOURCE_CRS,
        )

    buffered = lanes_source.geometry.buffer(
        buffer_m,
        cap_style=2,
        join_style=2,
    )

    merged = buffered.union_all()

    if merged.is_empty:
        polygons = []
    elif merged.geom_type == "Polygon":
        polygons = [merged]
    elif merged.geom_type == "MultiPolygon":
        polygons = list(merged.geoms)
    else:
        polygons = [
            geom for geom in getattr(merged, "geoms", [])
            if geom.geom_type == "Polygon"
        ]

    return gpd.GeoDataFrame(
        {"ROAD_POLYGON_ID": range(1, len(polygons) + 1)},
        geometry=polygons,
        crs=SOURCE_CRS,
    )


def _generate_centerlines_from_road_polygons(
    road_polygon_gdf,
    interpolation_distance=CENTERLINE_INTERPOLATION_DISTANCE,
):
    if Centerline is None:
        raise ImportError(
            "The centerline package is not installed. Install it with: "
            "pip install centerline networkx"
        )

    records = []

    for _, row in road_polygon_gdf.iterrows():
        polygon = row.geometry

        if polygon is None or polygon.is_empty:
            continue

        try:
            centreline_obj = Centerline(
                polygon,
                interpolation_distance=interpolation_distance,
            )
            centreline_geom = centreline_obj.geometry

        except Exception as exc:
            debug_log(
                f"Centerline failed for road polygon {row.get('ROAD_POLYGON_ID', '-')}",
                exc,
            )
            continue

        if centreline_geom is None or centreline_geom.is_empty:
            continue

        if centreline_geom.geom_type == "LineString":
            records.append({
                "road_polygon_id": row.get("ROAD_POLYGON_ID"),
                "geometry": centreline_geom,
            })

        elif centreline_geom.geom_type == "MultiLineString":
            for line in centreline_geom.geoms:
                if line is None or line.is_empty:
                    continue

                records.append({
                    "road_polygon_id": row.get("ROAD_POLYGON_ID"),
                    "geometry": line,
                })

    if not records:
        return gpd.GeoDataFrame(
            {"road_polygon_id": []},
            geometry=[],
            crs=SOURCE_CRS,
        )

    centreline_gdf = gpd.GeoDataFrame(
        records,
        geometry="geometry",
        crs=SOURCE_CRS,
    )

    return _explode_lines(centreline_gdf)


def _build_centerline_graph(centerline_gdf):
    graph = nx.Graph()
    segment_records = []

    for _, row in centerline_gdf.iterrows():
        line = row.geometry

        if line is None or line.is_empty:
            continue

        coords = list(line.coords)

        if len(coords) < 2:
            continue

        for i in range(len(coords) - 1):
            p1 = Point(coords[i])
            p2 = Point(coords[i + 1])

            if p1.distance(p2) == 0:
                continue

            node_a = _snap_point_key(p1)
            node_b = _snap_point_key(p2)

            if node_a == node_b:
                continue

            segment = LineString([node_a, node_b])
            direction_bin = _line_direction_bin(segment)

            graph.add_node(node_a, geometry=Point(node_a))
            graph.add_node(node_b, geometry=Point(node_b))

            graph.add_edge(
                node_a,
                node_b,
                geometry=segment,
                length=segment.length,
                direction_bin=direction_bin,
            )

            segment_records.append({
                "node_a": node_a,
                "node_b": node_b,
                "direction_bin": direction_bin,
                "geometry": segment,
            })

    segment_gdf = gpd.GeoDataFrame(
        segment_records,
        geometry="geometry",
        crs=SOURCE_CRS,
    )

    return graph, segment_gdf


def detect_intersections_from_centerlines(
    centerline_segments_gdf,
    graph,
    min_graph_degree=MIN_GRAPH_DEGREE,
    cluster_buffer_m=INTERSECTION_CLUSTER_BUFFER_METRES,
    polygon_radius_m=INTERSECTION_POLYGON_RADIUS_METRES,
    polygon_buffer_m=INTERSECTION_POLYGON_BUFFER_METRES,
):
    if centerline_segments_gdf.empty or graph.number_of_nodes() == 0:
        return empty_intersection_gdf()

    junction_records = []

    for node, degree in graph.degree():
        if degree < min_graph_degree:
            continue

        connected_direction_bins = []

        for neighbour in graph.neighbors(node):
            edge_data = graph.get_edge_data(node, neighbour)
            direction_bin = edge_data.get("direction_bin")

            if direction_bin is not None:
                connected_direction_bins.append(direction_bin)

        junction_records.append({
            "node": str(node),
            "GRAPH_DEGREE": int(degree),
            "NODE_DIRECTION_GROUP_COUNT": len(set(connected_direction_bins)),
            "geometry": Point(node),
        })

    if not junction_records:
        debug_log("No graph nodes with degree >= threshold were found")
        return empty_intersection_gdf()

    junction_gdf = gpd.GeoDataFrame(
        junction_records,
        geometry="geometry",
        crs=SOURCE_CRS,
    )

    dissolved = junction_gdf.geometry.buffer(cluster_buffer_m).union_all()

    if dissolved.is_empty:
        return empty_intersection_gdf()

    if dissolved.geom_type == "Polygon":
        cluster_geoms = [dissolved]
    else:
        cluster_geoms = list(dissolved.geoms)

    records = []

    for cluster_geom in cluster_geoms:
        cluster_nodes = junction_gdf[junction_gdf.geometry.within(cluster_geom)].copy()

        if cluster_nodes.empty:
            continue

        cluster_centre = cluster_nodes.geometry.union_all().centroid
        local_area = cluster_centre.buffer(polygon_radius_m)

        nearby_segments = centerline_segments_gdf[
            centerline_segments_gdf.geometry.intersects(local_area)
        ].copy()

        if nearby_segments.empty:
            continue

        branch_count = len(nearby_segments)
        direction_group_count = nearby_segments["direction_bin"].dropna().nunique()
        graph_node_count = len(cluster_nodes)
        max_graph_degree = int(cluster_nodes["GRAPH_DEGREE"].max())

        if branch_count < MIN_BRANCH_COUNT:
            continue

        if direction_group_count < MIN_DIRECTION_GROUPS:
            continue

        intersection_polygon = (
            nearby_segments.geometry
            .intersection(local_area)
            .buffer(polygon_buffer_m, cap_style=2, join_style=2)
            .union_all()
            .convex_hull
            .buffer(0)
        )

        if intersection_polygon.is_empty:
            continue

        marker_point = intersection_polygon.centroid

        records.append({
            "INTERSECTION_NAME": f"Centerline Intersection {len(records) + 1}",
            "INTERSECTION_X": marker_point.x,
            "INTERSECTION_Y": marker_point.y,
            "GRAPH_NODE_COUNT": int(graph_node_count),
            "MAX_GRAPH_DEGREE": int(max_graph_degree),
            "BRANCH_COUNT": int(branch_count),
            "DIRECTION_GROUP_COUNT": int(direction_group_count),
            "geometry": intersection_polygon,
        })

    if not records:
        debug_log("No centreline graph intersection clusters passed thresholds")
        return empty_intersection_gdf()

    result = gpd.GeoDataFrame(
        records,
        geometry="geometry",
        crs=SOURCE_CRS,
    )

    result = result.sort_values(
        ["DIRECTION_GROUP_COUNT", "BRANCH_COUNT", "MAX_GRAPH_DEGREE"],
        ascending=False,
    ).reset_index(drop=True)

    result["INTERSECTION_NAME"] = [
        f"Centerline Intersection {idx + 1}"
        for idx in range(len(result))
    ]

    result["DETECTION_METHOD"] = "CENTERLINE_GRAPH"

    debug_log(
        "Detected centreline graph intersections",
        result[[
            "INTERSECTION_NAME",
            "GRAPH_NODE_COUNT",
            "MAX_GRAPH_DEGREE",
            "BRANCH_COUNT",
            "DIRECTION_GROUP_COUNT",
        ]],
    )

    return result.to_crs(TARGET_CRS)


def detect_intersections_from_dxf_lanes(lane_gdf):
    """
    Detect intersections by converting lane LineStrings into road polygons,
    creating centrelines, building a graph, and finding compact graph junctions.
    This replaces the old ST_DWITHIN lane self-join detector.
    """
    if lane_gdf.empty:
        return empty_intersection_gdf()

    debug_log("Creating road polygons from lane buffers")
    road_polygon_gdf = _create_road_polygons_from_lanes(lane_gdf)
    debug_log("Road polygons", road_polygon_gdf)

    if road_polygon_gdf.empty:
        return empty_intersection_gdf()

    debug_log("Generating road centrelines")
    centerline_gdf = _generate_centerlines_from_road_polygons(road_polygon_gdf)
    debug_log("Generated centrelines", centerline_gdf)

    if centerline_gdf.empty:
        return empty_intersection_gdf()

    debug_log("Building centreline graph")
    graph, centerline_segments_gdf = _build_centerline_graph(centerline_gdf)
    debug_log("Graph node/edge count", {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
    })

    return detect_intersections_from_centerlines(
        centerline_segments_gdf=centerline_segments_gdf,
        graph=graph,
    )


def export_intersections_csv(intersection_gdf, output_path="detected_centerline_intersections.csv"):
    columns = [
        "INTERSECTION_NAME",
        "INTERSECTION_X",
        "INTERSECTION_Y",
        "GRAPH_NODE_COUNT",
        "MAX_GRAPH_DEGREE",
        "BRANCH_COUNT",
        "DIRECTION_GROUP_COUNT",
        "DETECTION_METHOD",
        "MANUAL_EDIT",
        "geometry_wkt",
    ]

    if intersection_gdf.empty:
        pd.DataFrame(columns=columns).to_csv(output_path, index=False)
        return

    export_df = intersection_gdf.copy()
    export_df["geometry_wkt"] = export_df.geometry.to_wkt()

    for column in columns:
        if column not in export_df.columns:
            export_df[column] = None

    export_df[columns].to_csv(output_path, index=False)



def apply_manual_intersection_edits(intersection_gdf):
    """
    Apply manual DELETE / MOVE / REPLACE / ADD rules to detected intersections.

    This is intentionally applied after the automatic detector, so you can keep
    improving the detector while still maintaining operationally approved
    polygon corrections.
    """
    expected_columns = [
        "INTERSECTION_NAME",
        "INTERSECTION_X",
        "INTERSECTION_Y",
        "GRAPH_NODE_COUNT",
        "MAX_GRAPH_DEGREE",
        "BRANCH_COUNT",
        "DIRECTION_GROUP_COUNT",
        "DETECTION_METHOD",
        "MANUAL_EDIT",
    ]

    if intersection_gdf is None or intersection_gdf.empty:
        edited = gpd.GeoDataFrame(
            {column: [] for column in expected_columns},
            geometry=[],
            crs=TARGET_CRS,
        )
    else:
        edited = intersection_gdf.copy()
        edited = gpd.GeoDataFrame(edited, geometry="geometry", crs=intersection_gdf.crs)
        edited = edited.to_crs(SOURCE_CRS)

    for column in expected_columns:
        if column not in edited.columns:
            edited[column] = None

    if not edited.empty:
        # DELETE: remove false-positive polygons by label.
        edited = edited[
            ~edited["INTERSECTION_NAME"].astype(str).isin(MANUAL_DELETE_INTERSECTIONS)
        ].copy()

        # REPLACE: replace polygon geometry entirely.
        for intersection_name, replacement_polygon in MANUAL_REPLACE_INTERSECTION_POLYGONS.items():
            mask = edited["INTERSECTION_NAME"].astype(str) == intersection_name

            if not mask.any():
                continue

            edited.loc[mask, "geometry"] = replacement_polygon
            edited.loc[mask, "MANUAL_EDIT"] = "REPLACE"
            edited.loc[mask, "DETECTION_METHOD"] = (
                edited.loc[mask, "DETECTION_METHOD"].fillna("AUTO").astype(str)
                + "+MANUAL_REPLACE"
            )
        # PRINT: print polygons for manual inspection.
        for intersection_name in PRINT_INTERSECTIONS_POLYGONS:
            mask = edited["INTERSECTION_NAME"].astype(str) == intersection_name

            if not mask.any():
                continue

            print(edited.loc[mask, ["INTERSECTION_NAME", "geometry"]].to_wkt())
            print(json.dumps(json.loads(edited.loc[mask, "geometry"].to_json()), indent=2))
        # MOVE: shift polygon by x/y metres.
        for intersection_name, offsets in MANUAL_MOVE_INTERSECTIONS.items():
            x_offset_m, y_offset_m = offsets
            mask = edited["INTERSECTION_NAME"].astype(str) == intersection_name

            if not mask.any():
                continue

            edited.loc[mask, "geometry"] = edited.loc[mask, "geometry"].apply(
                lambda geom: translate(
                    geom,
                    xoff=x_offset_m,
                    yoff=y_offset_m,
                )
            )
            edited.loc[mask, "MANUAL_EDIT"] = "MOVE"
            edited.loc[mask, "DETECTION_METHOD"] = (
                edited.loc[mask, "DETECTION_METHOD"].fillna("AUTO").astype(str)
                + "+MANUAL_MOVE"
            )

        # --------------------------------------------------
        # SWAP polygon geometries
        # --------------------------------------------------
        for name_a, name_b in MANUAL_SWAP_INTERSECTIONS:

            mask_a = edited["INTERSECTION_NAME"] == name_a
            mask_b = edited["INTERSECTION_NAME"] == name_b

            if not mask_a.any():
                continue

            if not mask_b.any():
                continue

            geometry_a = edited.loc[mask_a, "geometry"].iloc[0]
            geometry_b = edited.loc[mask_b, "geometry"].iloc[0]

            edited.loc[mask_a, "geometry"] = geometry_b
            edited.loc[mask_b, "geometry"] = geometry_a

            edited.loc[mask_a, "MANUAL_EDIT"] = "SWAP"
            edited.loc[mask_b, "MANUAL_EDIT"] = "SWAP"

            edited.loc[mask_a, "DETECTION_METHOD"] = (
                edited.loc[mask_a, "DETECTION_METHOD"]
                .fillna("AUTO")
                + "+MANUAL_SWAP"
            )

            edited.loc[mask_b, "DETECTION_METHOD"] = (
                edited.loc[mask_b, "DETECTION_METHOD"]
                .fillna("AUTO")
                + "+MANUAL_SWAP"
            )

        
        edited["INTERSECTION_X"] = edited.geometry.centroid.x
        edited["INTERSECTION_Y"] = edited.geometry.centroid.y

    # ADD: add missed intersections manually.
    add_records = []

    for intersection_name, polygon in MANUAL_ADD_INTERSECTION_POLYGONS:
        add_records.append({
            "INTERSECTION_NAME": intersection_name,
            "INTERSECTION_X": polygon.centroid.x,
            "INTERSECTION_Y": polygon.centroid.y,
            "GRAPH_NODE_COUNT": None,
            "MAX_GRAPH_DEGREE": None,
            "BRANCH_COUNT": None,
            "DIRECTION_GROUP_COUNT": None,
            "DETECTION_METHOD": "MANUAL_ADD",
            "MANUAL_EDIT": "ADD",
            "geometry": polygon,
        })

    if add_records:
        add_gdf = gpd.GeoDataFrame(add_records, geometry="geometry", crs=SOURCE_CRS)
        edited = pd.concat([edited, add_gdf], ignore_index=True)
        edited = gpd.GeoDataFrame(edited, geometry="geometry", crs=SOURCE_CRS)

    if edited.empty:
        return gpd.GeoDataFrame(
            {column: [] for column in expected_columns},
            geometry=[],
            crs=TARGET_CRS,
        )

    edited = edited[edited.geometry.notna() & ~edited.geometry.is_empty].copy()
    edited["INTERSECTION_X"] = edited.geometry.centroid.x
    edited["INTERSECTION_Y"] = edited.geometry.centroid.y
    # --------------------------------------------------
    # COPY existing intersection
    # --------------------------------------------------
    copied_rows = []

    for rule in MANUAL_COPY_INTERSECTIONS:

        source_name = rule["SOURCE"]
        new_name = rule["NEW_NAME"]

        xoff, yoff = rule.get("OFFSET_XY", (0, 0))

        source = edited[
            edited["INTERSECTION_NAME"] == source_name
        ]

        if source.empty:
            continue

        new_row = source.iloc[0].copy()

        new_row["INTERSECTION_NAME"] = new_name

        new_row["geometry"] = translate(
            new_row["geometry"],
            xoff=xoff,
            yoff=yoff,
        )

        new_row["INTERSECTION_X"] = new_row["geometry"].centroid.x
        new_row["INTERSECTION_Y"] = new_row["geometry"].centroid.y

        new_row["MANUAL_EDIT"] = "COPY"
        new_row["DETECTION_METHOD"] = "MANUAL_COPY"

        copied_rows.append(new_row)

    if copied_rows:

        copied_gdf = gpd.GeoDataFrame(
            copied_rows,
            geometry="geometry",
            crs=SOURCE_CRS,
        )

        edited = pd.concat(
            [edited, copied_gdf],
            ignore_index=True,
        )

        edited = gpd.GeoDataFrame(
            edited,
            geometry="geometry",
            crs=SOURCE_CRS,
        )
    debug_log(
        "Manual intersection edits applied",
        edited[[
            "INTERSECTION_NAME",
            "DETECTION_METHOD",
            "MANUAL_EDIT",
            "INTERSECTION_X",
            "INTERSECTION_Y",
        ]],
    )

    return edited.to_crs(TARGET_CRS)

def point_is_in_intersection_area(point_source, intersection_gdf):
    """Return True when a projected point is inside or within tolerance of an intersection."""
    if point_source is None or point_source.is_empty or intersection_gdf.empty:
        return False

    intersections_source = intersection_gdf.to_crs(SOURCE_CRS)

    return (
        intersections_source.geometry.contains(point_source).any()
        or (
            intersections_source.geometry.distance(point_source)
            <= INTERSECTION_POINT_TOLERANCE_METRES
        ).any()
    )


# -----------------------------
# GPKG For Intersections
# -----------------------------
def detect_intersections_from_gpkg(
    gpkg_path,
    endpoint_cluster_buffer_m=25,
    nearby_lane_radius_m=90,
    min_endpoint_count=3
):
    lanes = gpd.read_file(gpkg_path, layer="polylines")

    lanes = lanes[
        lanes["layer"].str.upper().str.contains("LANES", na=False)
    ].copy()

    lanes = lanes.set_crs(SOURCE_CRS, allow_override=True)

    endpoints = []

    for _, row in lanes.iterrows():
        geom = row.geometry

        if geom is None or geom.is_empty:
            continue

        coords = list(geom.coords)

        if len(coords) < 2:
            continue

        endpoints.append(Point(coords[0][0], coords[0][1]))
        endpoints.append(Point(coords[-1][0], coords[-1][1]))

    endpoint_gdf = gpd.GeoDataFrame(
        geometry=endpoints,
        crs=SOURCE_CRS
    )

    endpoint_clusters = gpd.GeoDataFrame(
        geometry=list(endpoint_gdf.buffer(endpoint_cluster_buffer_m).union_all().geoms),
        crs=SOURCE_CRS
    )

    candidate_polygons = []
    names = []

    for idx, cluster in endpoint_clusters.iterrows():
        endpoint_count = endpoint_gdf.within(cluster.geometry).sum()

        if endpoint_count < min_endpoint_count:
            continue

        centre = cluster.geometry.centroid
        nearby_lanes = lanes[
            lanes.geometry.distance(centre) <= nearby_lane_radius_m
        ]

        if nearby_lanes.empty:
            continue

        intersection_poly = (
            nearby_lanes.geometry
            .buffer(8)
            .union_all()
            .convex_hull
        )

        candidate_polygons.append(intersection_poly)
        names.append(f"Auto Intersection {idx + 1}")

    return gpd.GeoDataFrame(
        {"INTERSECTION_NAME": names},
        geometry=candidate_polygons,
        crs=SOURCE_CRS
    ).to_crs(TARGET_CRS)
# -----------------------------
# KML TO GEOJSON
# -----------------------------
def parse_coordinates(text):
    coords = []

    for point in text.strip().split():
        lon, lat, *_ = point.split(",")
        coords.append([float(lat), float(lon)])

    return coords


def kml_to_geojson(kml_file):
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    tree = etree.parse(kml_file)

    features = []

    for placemark in tree.xpath(".//kml:Placemark", namespaces=ns):
        name = placemark.findtext("kml:name", namespaces=ns)

        polygon_nodes = placemark.xpath(
            ".//kml:Polygon//kml:coordinates",
            namespaces=ns
        )

        line_nodes = placemark.xpath(
            ".//kml:LineString//kml:coordinates",
            namespaces=ns
        )

        if polygon_nodes:
            coords = parse_coordinates(polygon_nodes[0].text)
            coords_lonlat = [[lonlat[1], lonlat[0]] for lonlat in coords]

            features.append({
                "type": "Feature",
                "properties": {"name": name},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords_lonlat],
                },
            })

        if line_nodes:
            coords = parse_coordinates(line_nodes[0].text)
            coords_lonlat = [[lonlat[1], lonlat[0]] for lonlat in coords]

            features.append({
                "type": "Feature",
                "properties": {"name": name},
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords_lonlat,
                },
            })

    return {
        "type": "FeatureCollection",
        "features": features
    }


# -----------------------------
# SNOWFLAKE DATA LOADER
# -----------------------------
def load_nearby_machine_data(start_date, end_date):

    engine = create_engine('snowflake://{user}:{password}@{account}/{database}/{schema}?warehouse={warehouse}&role={role}&authenticator=externalbrowser'.format(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password = "",
            authenticator=os.getenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
            role=os.environ["SNOWFLAKE_ROLE"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=os.environ["SNOWFLAKE_DATABASE"],
            schema=os.environ["SNOWFLAKE_SCHEMA"],
        ))

    hov_type_sql = "'DZ', 'WD', 'WC', 'GR', 'LV', 'EL', 'WL', 'EX'"
    # , 'GR', 'LV', 'EL', 'WL', 'EX'

    query = f"""
WITH base AS (
    SELECT
        *,
        DATE_TRUNC('SECOND', "TIMESTAMP") AS TS_SECOND,

        LAG(X) OVER (
            PARTITION BY MACHINE
            ORDER BY "TIMESTAMP"
        ) AS PREV_X,

        LAG(Y) OVER (
            PARTITION BY MACHINE
            ORDER BY "TIMESTAMP"
        ) AS PREV_Y,

        LAG(Z) OVER (
            PARTITION BY MACHINE
            ORDER BY "TIMESTAMP"
        ) AS PREV_Z

    FROM AA_OPERATIONS_MANAGEMENT.SELFSERVICE.FMS_MACHINE_LOCATION
    WHERE "TIMESTAMP" BETWEEN '{start_date}' AND '{end_date}'
),

movement AS (
    SELECT
        *,
        CASE
            WHEN MACHINE LIKE 'ELI%' THEN 'ELI'
            ELSE LEFT(MACHINE, 2)
        END AS ASSET_PREFIX,
        CASE
            WHEN PREV_X IS NULL THEN 0
            WHEN SQRT(
                POWER(X - PREV_X, 2) +
                POWER(Y - PREV_Y, 2) +
                POWER(Z - PREV_Z, 2)
            ) > 0.5 THEN 1
            ELSE 0
        END AS IS_MOVING
    FROM base
),

ht AS (
    SELECT *
    FROM movement
    WHERE ASSET_PREFIX ='DT'
),

hov AS (
    SELECT *
    FROM movement
    WHERE ASSET_PREFIX IN ({hov_type_sql})
),

matches AS (
    SELECT
        h.MACHINE AS HT_MACHINE,
        l.MACHINE AS HOV_MACHINE,

        h.ASSET_PREFIX AS HT_PREFIX,
        l.ASSET_PREFIX AS HOV_PREFIX,

        TO_CHAR(h.TS_SECOND, 'YYYY-MM-DD HH24:MI:SS') AS "TIMESTAMP",

        ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) AS DISTANCE_METRES,

        CASE
            WHEN ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) < 15
                THEN 'NEAR_MISS'
            WHEN ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) <= 50
                THEN 'PROCEDURE_BREACH'
        END AS INTERACTION_TYPE,

        h.X AS HT_X,
        h.Y AS HT_Y,
        h.Z AS HT_Z,
        h.WKT_GEOM AS HT_WKT_GEOM,
        h.THE_GEOM AS HT_GEOM,

        l.X AS HOV_X,
        l.Y AS HOV_Y,
        l.Z AS HOV_Z,
        l.WKT_GEOM AS HOV_WKT_GEOM,
        l.THE_GEOM AS HOV_GEOM,

        h.IS_MOVING AS HT_IS_MOVING,
        l.IS_MOVING AS HOV_IS_MOVING,

        CASE
            WHEN h.IS_MOVING = 1 AND l.IS_MOVING = 1 THEN 'BOTH_MOVING'
            WHEN h.IS_MOVING = 1 THEN 'HT_DT_MOVING'
            WHEN l.IS_MOVING = 1 THEN 'HOV_MOVING'
        END AS MOVING_STATUS

    FROM ht h
    JOIN hov l
        ON h.TS_SECOND = l.TS_SECOND
       AND ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) <= 50

    WHERE h.IS_MOVING = 1
       and l.IS_MOVING = 1
)

SELECT *
FROM matches
ORDER BY "TIMESTAMP", DISTANCE_METRES;
    """

    with engine.connect() as connection:
        df = pd.read_sql(query, connection).rename(columns=str.upper)
    debug_log("Snowflake rows returned", df)
    return df


# -----------------------------
# PROCESS HT-HOV MATCH DATA
# -----------------------------
def load_intersection_boxes():
    names = []
    polygons = []

    for name, polygon in INTERSECTION_POLYGONS:
        names.append(name)
        polygons.append(polygon)

    return gpd.GeoDataFrame(
        {"INTERSECTION_NAME": names},
        geometry=polygons,
        crs=SOURCE_CRS
    ).to_crs(TARGET_CRS)


def classify_area_categories(gdf, lane_gdf, intersection_gdf):
    """Classify points using GeoPandas spatial indexes instead of Python loops."""
    if gdf.empty:
        return gdf

    points_source = gdf.to_crs(SOURCE_CRS).copy()
    points_source["_ROW_ID"] = points_source.index

    lanes_source = lane_gdf.to_crs(SOURCE_CRS)[
        ["polyline_id", "layer", "vertex_count", "geometry"]
    ].copy()
    lanes_source = lanes_source.rename(columns={
        "layer": "lane_layer",
        "vertex_count": "lane_vertex_count",
    })

    if lanes_source.empty:
        points_source["DISTANCE_TO_LANE_M"] = float("nan")
        points_source["polyline_id"] = None
        points_source["lane_layer"] = None
        points_source["lane_vertex_count"] = None
    else:
        nearest = gpd.sjoin_nearest(
            points_source[["_ROW_ID", "geometry"]],
            lanes_source,
            how="left",
            distance_col="DISTANCE_TO_LANE_M",
        )
        nearest = (
            nearest.sort_values(["_ROW_ID", "DISTANCE_TO_LANE_M"])
            .drop_duplicates("_ROW_ID", keep="first")
            .set_index("_ROW_ID")
        )
        for column in [
            "DISTANCE_TO_LANE_M",
            "polyline_id",
            "lane_layer",
            "lane_vertex_count",
        ]:
            points_source[column] = points_source["_ROW_ID"].map(nearest[column])

    points_source["ON_LANE"] = (
        points_source["DISTANCE_TO_LANE_M"].fillna(float("inf"))
        <= LANE_BUFFER_METRES
    )
    points_source["AREA_CATEGORY"] = "dynamic area"
    points_source.loc[points_source["ON_LANE"], "AREA_CATEGORY"] = "haul road"
    points_source["INTERSECTION_NAME"] = None

    if intersection_gdf is not None and not intersection_gdf.empty:
        intersections_source = intersection_gdf.to_crs(SOURCE_CRS)[
            ["INTERSECTION_NAME", "geometry"]
        ].copy()
        # Buffer once, then use a spatial-indexed join for contains/near tolerance.
        intersections_source["geometry"] = intersections_source.geometry.buffer(
            INTERSECTION_POINT_TOLERANCE_METRES
        )
        hits = gpd.sjoin(
            points_source[["_ROW_ID", "geometry"]],
            intersections_source,
            how="left",
            predicate="within",
        )
        hits = hits.dropna(subset=["INTERSECTION_NAME"])
        if not hits.empty:
            first_hits = hits.drop_duplicates("_ROW_ID", keep="first").set_index("_ROW_ID")
            hit_names = points_source["_ROW_ID"].map(first_hits["INTERSECTION_NAME"])
            hit_mask = hit_names.notna()
            points_source.loc[hit_mask, "AREA_CATEGORY"] = "intersection"
            points_source.loc[hit_mask, "INTERSECTION_NAME"] = hit_names[hit_mask]

    classified = points_source.to_crs(TARGET_CRS)
    classified.index = gdf.index
    classified = classified.drop(columns=["_ROW_ID"], errors="ignore")

    classified = classified[
        ~(
            classified["TYPE"].eq("HOV")
            & classified["PREFIX"].eq("WL")
            & classified["AREA_CATEGORY"].eq("dynamic area")
        )
    ].copy()

    debug_log("classify_area_categories", classified)
    return classified


def process_pair_data(df):
    if df.empty:
        return gpd.GeoDataFrame(geometry=[], crs=TARGET_CRS)

    source = df.copy()
    source["TIMESTAMP"] = pd.to_datetime(source["TIMESTAMP"], errors="coerce")
    source = source.dropna(subset=["TIMESTAMP", "HT_WKT_GEOM", "HOV_WKT_GEOM"])

    # Vectorized WKT parsing is substantially faster than Series.apply(wkt.loads).
    try:
        from shapely import from_wkt
        source["HT_GEOMETRY"] = from_wkt(source["HT_WKT_GEOM"].to_numpy())
        source["HOV_GEOMETRY"] = from_wkt(source["HOV_WKT_GEOM"].to_numpy())
    except ImportError:
        source["HT_GEOMETRY"] = source["HT_WKT_GEOM"].map(wkt.loads)
        source["HOV_GEOMETRY"] = source["HOV_WKT_GEOM"].map(wkt.loads)

    common = [
        "TIMESTAMP", "DISTANCE_METRES", "INTERACTION_TYPE", "MOVING_STATUS"
    ]

    ht_df = source[[
        "HT_MACHINE", "HT_PREFIX", "HOV_MACHINE", "HOV_PREFIX", *common,
        "HT_IS_MOVING", "HOV_IS_MOVING", "HT_X", "HT_Y", "HT_Z", "HT_GEOMETRY"
    ]].copy()
    ht_df.columns = [
        "MACHINE", "PREFIX", "PAIR_MACHINE", "PAIR_PREFIX", *common,
        "IS_MOVING", "PAIR_IS_MOVING", "X", "Y", "Z", "geometry"
    ]
    ht_df["TYPE"] = "HT"

    hov_df = source[[
        "HOV_MACHINE", "HOV_PREFIX", "HT_MACHINE", "HT_PREFIX", *common,
        "HOV_IS_MOVING", "HT_IS_MOVING", "HOV_X", "HOV_Y", "HOV_Z", "HOV_GEOMETRY"
    ]].copy()
    hov_df.columns = [
        "MACHINE", "PREFIX", "PAIR_MACHINE", "PAIR_PREFIX", *common,
        "IS_MOVING", "PAIR_IS_MOVING", "X", "Y", "Z", "geometry"
    ]
    hov_df["TYPE"] = "HOV"

    plot_df = pd.concat([ht_df, hov_df], ignore_index=True).drop_duplicates(
        subset=["TYPE", "MACHINE", "PAIR_MACHINE", "TIMESTAMP", "X", "Y"]
    )

    gdf_source = gpd.GeoDataFrame(plot_df, geometry="geometry", crs=SOURCE_CRS)
    gdf_source.sort_values(["TYPE", "MACHINE", "PAIR_MACHINE", "TIMESTAMP"], inplace=True)

    min_timestamp = gdf_source["TIMESTAMP"].min()
    gdf_source["TIME_INT"] = (
        gdf_source["TIMESTAMP"] - min_timestamp
    ).dt.total_seconds().astype("int32")
    gdf_source["TIMESTAMP_STR"] = gdf_source["TIMESTAMP"].dt.strftime("%Y-%m-%d %H:%M:%S")

    classified = classify_area_categories(gdf_source, dxf_gdf, intersection_gdf)
    classified["lon"] = classified.geometry.x
    classified["lat"] = classified.geometry.y
    debug_log("process_pair_data", classified)
    return classified


@lru_cache(maxsize=8)
def get_processed_pair_data(start_date, end_date):
    """Cache Snowflake and geospatial processing by exact date range."""
    raw = load_nearby_machine_data(start_date, end_date)
    return process_pair_data(raw)


def get_processed_pair_data_copy(start_date, end_date):
    # Callbacks filter the result, so return a shallow copy to protect the cache.
    return get_processed_pair_data(start_date, end_date).copy()


# -----------------------------
# LOAD STATIC MAP DATA
# -----------------------------
geojson_data = kml_to_geojson(KML_FILE)
dxf_gdf = load_dxf_lanes(DXF_PATH)
manual_intersections_gdf = load_intersection_boxes()
dxf_intersections_gdf = detect_intersections_from_dxf_lanes(dxf_gdf)
# export_intersections_csv(dxf_intersections_gdf)

# Keep the manually confirmed polygons and add the automatically detected
# ST_DWITHIN-style lane-pair clusters. Do not include the old GPKG endpoint
# detector here because it can over-detect ordinary haul-road joins.
intersection_gdf = pd.concat(
    [manual_intersections_gdf, dxf_intersections_gdf],
    ignore_index=True
)
intersection_gdf = gpd.GeoDataFrame(
    intersection_gdf,
    geometry="geometry",
    crs=TARGET_CRS
)

# Apply manual corrections after automatic detection and manual base polygons.
# This lets you remove false positives, move polygons, replace polygons,
# or add missed intersections from the CONFIG section above.
intersection_gdf = apply_manual_intersection_edits(intersection_gdf)
# export_intersections_csv(intersection_gdf, "final_intersections_after_manual_edits.csv")

static_layers = []

for geom in dxf_gdf.geometry:
    if geom is None or geom.is_empty:
        continue

    x, y = geom.xy

    lane_positions = [
        [lat, lon]
        for lon, lat in zip(x, y)
    ]

    static_layers.append(
        dl.Polyline(
            positions=lane_positions,
            color="white",
            weight=1,
            opacity=0.7
        )
    )

for _, row in intersection_gdf.iterrows():
    if row.geometry is None or row.geometry.is_empty:
        continue

    if row.geometry.geom_type == "Polygon":
        polygons = [row.geometry]
    elif row.geometry.geom_type == "MultiPolygon":
        polygons = list(row.geometry.geoms)
    else:
        continue

    for polygon in polygons:
        x, y = polygon.exterior.xy
        polygon_positions = [[lat, lon] for lon, lat in zip(x, y)]

        static_layers.append(
            dl.Polygon(
                positions=polygon_positions,
                color="orange",
                weight=2,
                fill=True,
                fillOpacity=0.15,
                children=[
                    dl.Tooltip([
                        html.Div(f"Intersection: {row.get('INTERSECTION_NAME', '-') }"),
                        html.Div(f"Graph nodes: {row.get('GRAPH_NODE_COUNT', '-') }"),
                        html.Div(f"Max graph degree: {row.get('MAX_GRAPH_DEGREE', '-') }"),
                        html.Div(f"Branches: {row.get('BRANCH_COUNT', '-') }"),
                        html.Div(f"Direction groups: {row.get('DIRECTION_GROUP_COUNT', '-') }"),
                        html.Div(f"Method: {row.get('DETECTION_METHOD', '-') }"),
                        html.Div(f"Manual edit: {row.get('MANUAL_EDIT', '-') }"),
                    ])
                ],
            )
        )

    # centre = polygon.centroid
    # intersection_name = row.get("INTERSECTION_NAME", "-")

    # static_layers.append(
    #     dl.DivMarker(
    #         position=[centre.y, centre.x],
    #         iconOptions={
    #             "html": (
    #                 "<div style='"
    #                 "background: orange;"
    #                 "color: black;"
    #                 "font-size: 11px;"
    #                 "font-weight: bold;"
    #                 "padding: 2px 5px;"
    #                 "border: 1px solid black;"
    #                 "border-radius: 4px;"
    #                 "white-space: nowrap;"
    #                 "'>"
    #                 f"{intersection_name}"
    #                 "</div>"
    #             ),
    #             "className": "intersection-label",
    #             "iconSize": [120, 22],
    #             "iconAnchor": [60, 11],
    #         },
    #     )
    # )


# -----------------------------
# DASH APP
# -----------------------------
app = Dash(__name__)

app.layout = html.Div(
    style={"height": "100vh"},
    children=[

        html.Div(
            "Eliwana Haul Road - HT/DT/HOV Interactions Within 50 m",
            style={
                "padding": "10px",
                "fontSize": "20px",
                "fontWeight": "bold"
            }
        ),

        html.Div(
            style={"padding": "10px"},
            children=[

                html.Label("Start Datetime"),

                dcc.Input(
                    id="start-datetime",
                    type="text",
                    value=DEFAULT_START_DATETIME,
                    placeholder="YYYY-MM-DD HH:MM:SS",
                    style={
                        "marginRight": "20px",
                        "width": "180px"
                    }
                ),

                html.Label("End Datetime"),

                dcc.Input(
                    id="end-datetime",
                    type="text",
                    value=DEFAULT_END_DATETIME,
                    placeholder="YYYY-MM-DD HH:MM:SS",
                    style={"width": "180px"}
                )
            ]
        ),

        # html.Div(
        #     style={"padding": "10px"},
        #     children=[
        #         html.Label("Distance Threshold"),

        #         dcc.Dropdown(
        #             id="distance-dropdown",
        #             options=[
        #                 {"label": "10 m", "value": 10},
        #                 {"label": "20 m", "value": 20},
        #                 {"label": "50 m", "value": 50},
        #             ],
        #             value=50,
        #             clearable=False
        #         )
        #     ]
        # ),

        html.Div(
            style={"padding": "10px"},
            children=[
                html.Label("Select Asset types"),

                dcc.Dropdown(
                    id="machine-dropdown",
                    options=[],
                    value=[],
                    multi=True
                )
            ]
        ),

        html.Div(
            style={"padding": "10px"},
            children=[
                html.Label("Time Range"),
                dcc.RangeSlider(
                    id="time-slider",
                    min=0,
                    max=1,
                    value=[0, 1],
                    marks={0: "Start", 1: "End"},
                    step=60,
                    allowCross=False
                )
            ]
        ),

        html.Div(
            style={
                "display": "flex",
                "flexWrap": "wrap",
                "gap": "18px",
                "padding": "8px 12px",
                "alignItems": "center",
                "fontWeight": "bold",
                "backgroundColor": "rgba(255, 255, 255, 0.92)",
                "borderTop": "1px solid #d0d0d0",
                "borderBottom": "1px solid #d0d0d0",
            },
            children=[
                html.Span("Operational Area:"),
                html.Span([
                    html.Span(
                        style={
                            "display": "inline-block",
                            "width": "12px",
                            "height": "12px",
                            "backgroundColor": AREA_COLORS["haul road"],
                            "border": "1px solid black",
                            "marginRight": "5px",
                            "verticalAlign": "middle",
                        }
                    ),
                    "Haul Road",
                ]),
                html.Span([
                    html.Span(
                        style={
                            "display": "inline-block",
                            "width": "12px",
                            "height": "12px",
                            "backgroundColor": AREA_COLORS["intersection"],
                            "border": "1px solid black",
                            "marginRight": "5px",
                            "verticalAlign": "middle",
                        }
                    ),
                    "Intersection",
                ]),
                html.Span([
                    html.Span(
                        style={
                            "display": "inline-block",
                            "width": "12px",
                            "height": "12px",
                            "backgroundColor": AREA_COLORS["dynamic area"],
                            "border": "1px solid black",
                            "marginRight": "5px",
                            "verticalAlign": "middle",
                        }
                    ),
                    "Dynamic Area",
                ]),
                html.Span([
                    html.Span(
                        style={
                            "display": "inline-block",
                            "width": "12px",
                            "height": "12px",
                            "backgroundColor": DEFAULT_AREA_COLOR,
                            "border": "1px solid black",
                            "marginRight": "5px",
                            "verticalAlign": "middle",
                        }
                    ),
                    "Unclassified",
                ]),
            ],
        ),

        dl.Map(
            id="trajectory-map",
            center=[-22.0, 119.0],
            zoom=18,
            crs="EPSG4326",
            children=[

                dl.WMSTileLayer(
                    url=WMS_URL,
                    layers="BaseGlobe.I.tbp",
                    format="image/jpeg",
                    transparent=False,
                    version="1.3.0",
                    attribution="SkylineGlobe Server",
                ),

                dl.GeoJSON(
                    data=geojson_data,
                    zoomToBounds=True,
                    options={
                        "style": {
                            "color": "white",
                            "weight": 0,
                            "fillOpacity": 0,
                        }
                    },
                ),

                dl.LayerGroup(children=static_layers),

                dl.LayerGroup(id="dynamic-layers"),
            ],
            style={
                "height": "80vh",
                "width": "100%",
                "margin": "0",
            },
        )
    ]
)


# -----------------------------
# UPDATE MACHINE DROPDOWN + TIME SLIDER
# -----------------------------
@app.callback(
    Output("machine-dropdown", "options"),
    Output("machine-dropdown", "value"),
    Output("time-slider", "min"),
    Output("time-slider", "max"),
    Output("time-slider", "value"),
    Output("time-slider", "marks"),

    Input("start-datetime", "value"),
    Input("end-datetime", "value"),
    # Input("distance-dropdown", "value")
)
def update_controls(start_date, end_date):

    start_date = clean_datetime(start_date, DEFAULT_START_DATETIME)
    end_date = clean_datetime(end_date, DEFAULT_END_DATETIME)

    gdf_plot = get_processed_pair_data_copy(start_date, end_date)
    debug_log("Update Control", gdf_plot)
    if gdf_plot.empty:
        return [], [], 0, 1, [0, 1], {0: "No data"}
    machines = sorted(
        gdf_plot.loc[gdf_plot["TYPE"] == "HOV", "PREFIX"].unique()
    )

    machine_options = [
        {"label": m, "value": m}
        for m in machines
    ]

    min_time = int(gdf_plot["TIME_INT"].min())
    max_time = int(gdf_plot["TIME_INT"].max())

    num_marks = 8
    step = max((max_time - min_time) // num_marks, 1)

    time_marks = {}

    for t in range(min_time, max_time + 1, step):
        label_time = gdf_plot["TIMESTAMP"].min() + pd.Timedelta(seconds=t)
        time_marks[t] = label_time.strftime("%H:%M")

    return (
        machine_options,
        machines,
        min_time,
        max_time,
        [min_time, max_time],
        time_marks
    )


# # -----------------------------
# # UPDATE MAP
# # -----------------------------
@app.callback(
    Output("dynamic-layers", "children"),

    Input("machine-dropdown", "value"),
    Input("time-slider", "value"),
    # Input("distance-dropdown", "value"),
    Input("start-datetime", "value"),
    Input("end-datetime", "value")
)
def update_map(selected_machines, time_range, start_date, end_date):
    if not selected_machines:
        return []

    start_date = clean_datetime(start_date, DEFAULT_START_DATETIME)
    end_date = clean_datetime(end_date, DEFAULT_END_DATETIME)
    start_time, end_time = time_range
    gdf_plot = get_processed_pair_data_copy(start_date, end_date)
    if gdf_plot.empty:
        return []

    gdf_plot = gdf_plot[
        (
            (
                (gdf_plot["TYPE"] == "HT") &
                (gdf_plot["PAIR_PREFIX"].isin(selected_machines))
            ) |
            (
                (gdf_plot["TYPE"] == "HOV") &
                (gdf_plot["PREFIX"].isin(selected_machines))
            )
        ) &
        
            (gdf_plot["TIME_INT"] >= start_time) &
            (gdf_plot["TIME_INT"] <= end_time) 
    ].copy()

    # Save the same records currently displayed on the map.
    # The CSV includes the nearest DXF lane polyline_id.
    export_df = gdf_plot.copy()
    export_df["geometry_wkt"] = export_df.geometry.to_wkt()
    # export_df.drop(columns="geometry").to_csv(
    #     "map_filtered_interactions.csv",
    #     index=False
    # )

    if gdf_plot.empty:
        return []

    layers = []

    grouped = {
        (machine_type, prefix): group.sort_values("TIME_INT")
        for (machine_type, prefix), group in gdf_plot.groupby(["TYPE", "PREFIX"])
    }

    for _, group in grouped.items():
        group = group.dropna(subset=["lon", "lat"])

        if group.empty:
            continue

        for _, row in group.iterrows():
            area_category = str(
                row.get("AREA_CATEGORY", "")
            ).strip().lower()

            marker_color = AREA_COLORS.get(
                area_category,
                DEFAULT_AREA_COLOR,
            )

            layers.append(
                dl.CircleMarker(
                    id=(
                        f"{row['TYPE']}_"
                        f"{row['MACHINE']}_"
                        f"{row['PAIR_MACHINE']}_"
                        f"{row['TIME_INT']}_"
                        f"{row['lat']:.6f}_"
                        f"{row['lon']:.6f}"
                    ),
                    center=[row["lat"], row["lon"]],
                    radius=4,
                    color=marker_color,
                    fillColor=marker_color,
                    fill=True,
                    fillOpacity=0.85,
                    weight=2,
                    children=[
                        dl.Tooltip([
                            html.Div(f"Type: {row['TYPE']}"),
                            html.Div(f"Machine: {row['MACHINE']}"),
                            html.Div(f"Asset Type: {row['PREFIX']}"),
                            html.Div(f"Near Machine: {row['PAIR_MACHINE']}"),
                            html.Div(f"Near Asset Type: {row['PAIR_PREFIX']}"),
                            html.Div(f"Time: {row['TIMESTAMP']}"),
                            html.Div(f"Distance: {row['DISTANCE_METRES']:.2f} m"),
                            html.Div(f"Interaction: {row['INTERACTION_TYPE']}"),
                            html.Div(f"Moving: {row['MOVING_STATUS']}"),
                            html.Div(f"Operational Area: {str(row['AREA_CATEGORY']).title()}"),
                            html.Div(f"Intersection: {row['INTERSECTION_NAME'] or '-'}"),
                            html.Div(f"Distance to lane: {row['DISTANCE_TO_LANE_M']:.2f} m"),
                            html.Div(f"X: {row['X']:.2f}"),
                            html.Div(f"Y: {row['Y']:.2f}")
                        ])
                    ]
                )
            )

    return layers


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, port=8002)