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
    from .geometry import 
except ImportError:
     = None


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

DEFAULT_START_DATETIME = "2026-06-01 11:00:00"
DEFAULT_END_DATETIME = "2026-06-01 11:30:00"

# DEFAULT_START_DATETIME = "2026-06-01 00:00:00"
# DEFAULT_END_DATETIME = "2026-06-02 00:00:00"


HOV_ASSET_TYPES = ["DZ", "WD", "WC", "GR", "LV", "ELI", "WL", "EX"]

#Intersection detection approach:
#   1. buffer lane LineStrings to create road-surface polygons,
#   2. generate road centrelines from those polygons,
#   3. build a graph from centreline segments,
#   4. detect compact clusters of graph nodes with degree >= 3,
#   5. create polygons around the local centreline branches.
# This avoids falseIntersections caused by many parallel lane self-join rows.

# LANE_BUFFER_METRES = 10
LANE_BUFFER_METRES = 12
INTERSECTION_POINT_TOLERANCE_METRES = 10

ROAD_POLYGON_BUFFER_METRES = 8
_INTERPOLATION_DISTANCE = 5
NODE_SNAP_METRES = 5
# NODE_SNAP_METRES = 8
MIN_GRAPH_DEGREE = 3
INTERSECTION_CLUSTER_BUFFER_METRES = 30
#Intersection_CLUSTER_BUFFER_METRES = 35
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

# DefineIntersection boxes in SOURCE_CRS metres: (name, min_x, min_y, max_x, max_y).
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
# MANUALIntersection EDITS
# -----------------------------
# Use this section to correct automatically detectedIntersection polygons.
# All offsets are in SOURCE_CRS metres, so:
#   x_offset_m < 0 = move west/left
#   x_offset_m > 0 = move east/right
#   y_offset_m < 0 = move south/down
#   y_offset_m > 0 = move north/up
#
# Example from your screenshot:
#   - removeIntersection 56, 162, and 372
#   - moveIntersection 70 and 161 slightly down-left
#
# You can add more names here after checking the map labels.
MANUAL_DELETE_INTERSECTIONS = {
    "Intersection 56",
    "Intersection 162",
    "Intersection 372",
    "Intersection 261",
    "Intersection 369",
    "Intersection 361",
    "Intersection 387",
    "Intersection 318",
    "Intersection 377",
    "Intersection 117",
    "Intersection 50",
    "Intersection 364",
    "Intersection 198",
    "Intersection 165",
    "Intersection 4",
    "Intersection 348",
    "Intersection 311",
    "Intersection 159",
    "Intersection 88",
    "Intersection 320",
    "Intersection 125",
    "Intersection 373",
    "Intersection 15",
    "Intersection 384",
    "Intersection 319",
    "Intersection 101",
    "Intersection 323",
    "Intersection 322",
    "Intersection 226",
    "Intersection 294",
    "Intersection 138",
    "Intersection 388",
    "Intersection 290",
    "Intersection 308",
    "Intersection 97",
    "Intersection 362",
    "Intersection 157",
    "Intersection 24",
    "Intersection 386",
    "Intersection 237",
    "Intersection 254",
    "Intersection 378",
    "Intersection 49",
    "Intersection 297",
    "Intersection 356",
    "Intersection 74",
    "Intersection 349",
    "Intersection 355",
    "Intersection 279",
    "Intersection 295",
    "Intersection 288",
    "Intersection 371",
    "Intersection 266",
    "Intersection 128",
    "Intersection 240",
    "Intersection 370",
    "Intersection 291",
    "Intersection 337",
    "Intersection 260",
    "Intersection 363",
    "Intersection 306",
    "Intersection 276",
    "Intersection 352",
    "Intersection 140",
    "Intersection 289",
    "Intersection 228",
    "Intersection 95",
    "Intersection 277",
    "Intersection 137",
    "Intersection 136",
    "Intersection 324",
    "Intersection 258",
    "Intersection 119",
    "Intersection 132",
    "Intersection 16",
    "Intersection 66",
    "Intersection 139",
    "Intersection 28",
    "Intersection 229",
    "Intersection 123",
    "Intersection 133",
    "Intersection 292",
    "Intersection 2",
    "Intersection 328",
    "Intersection 251",
    "Intersection 264",
    "Intersection 127",
    "Intersection 124",
    "Intersection 293",
    "Intersection 310",
    "Intersection 121",
    "Intersection 205",
    "Intersection 23",
    "Intersection 262",
    "Intersection 71",
    "Intersection 3",
    "Intersection 122",
    "Intersection 304",
    "Intersection 232",
    "Intersection 154",
    "Intersection 309",
    "Intersection 231",
    "Intersection 389",
    "Intersection 244",
    "Intersection 179",
    "Intersection 332",
    "Intersection 259",
    "Intersection 330",
    "Intersection 333",
    "Intersection 338",
    "Intersection 343",
    "Intersection 177",
    "Intersection 267",
    "Intersection 112",
    "Intersection 134",
    "Intersection 298",
    "Intersection 350",
    "Intersection 255",
    "Intersection 314",
    "Intersection 45",
    "Intersection 82",
    "Intersection 313",
    "Intersection 357",
    "Intersection 256",
    "Intersection 169",
    "Intersection 72",
    "Intersection 147",
    "Intersection 152",
    "Intersection 347",
    "Intersection 336",
    "Intersection 217",
    "Intersection 367",
    "Intersection 280",
    "Intersection 73",
    "Intersection 46",
    "Intersection 61",
    "Intersection 43",
    "Intersection 346",
    "Intersection 35",
    "Intersection 38",
    "Intersection 39",
    "Intersection 144",
    "Intersection 149",
    "Intersection 211",
    "Intersection 41",
    "Intersection 151",
    "Intersection 37",
    "Intersection 40",
    "Intersection 42",
    "Intersection 145",
    "Intersection 374",
    "Intersection 334",
    "Intersection 331",
    "Intersection 153",
    "Intersection 385",
    "Intersection 69",
    "Intersection 146",
    "Intersection 247",
    "Intersection 234",
    "Intersection 214",
    "Intersection 235",
    "Intersection 246",
    "Intersection 111",
    "Intersection 43",
    "Intersection 265",
    "Intersection 253",
    "Intersection 296",
    "Intersection 354",
    "Intersection 312",
    "Intersection 329",
    "Intersection 344",
    "Intersection 365",
    "Intersection 163",
    "Intersection 358",
    "Intersection 52",
    "Intersection 65",
    "Intersection 68",
    "Intersection 148",
    "Intersection 54",
    "Intersection 381",
    "Intersection 143",
    "Intersection 257",
    "Intersection 300",
    "Intersection 284",
    "Intersection 171",
    "Intersection 170",
    "Intersection 194",
    "Intersection 7",
    "Intersection 84",
    "Intersection 62",
    "Intersection 250",
    "Intersection 172",
    "Intersection 273",
    "Intersection 248",
    "Intersection 212",
    "Intersection 193",
    "Intersection 285",
    "Intersection 221",
    "Intersection 382",
    "Intersection 316",
    "Intersection 202",
    "Intersection 383",
    "Intersection 130",
    "Intersection 287",
    "Intersection 20",
    "Intersection 302",
    "Intersection 286",
    "Intersection 317",
    "Intersection 274",
    "Intersection 53",
    "Intersection 325",
    "Intersection 303",
    "Intersection 376",
    "Intersection 390",
    "Intersection 335",
    "Intersection 185",
    "Intersection 142",
    "Intersection 305",
    "Intersection 281",
    "Intersection 282",
    "Intersection 63",
    "Intersection 299",
    "Intersection 368",
    "Intersection 375",
    "Intersection 223",
    "Intersection 321",
    "Intersection 380",
    "Intersection 360",
    "Intersection 307",
    "Intersection 110",
    "Intersection 1",
    "Intersection 64",
    "Intersection 60",
    "Intersection 160",
    "Intersection 327",
    "Intersection 190",
    "Intersection 366",
    "Intersection 379",
    "Intersection 85",
    "Intersection 252",
    "Intersection 141",
    "Intersection 34",
    "Intersection 315",
    "Intersection 118",
    "Intersection 51",
    "Intersection 271",
    "Intersection 391",
    "Intersection 249",
    "Intersection 126",
    "Intersection 92",
    "Intersection 239",
    "Intersection 359",
    "Intersection 351",
    "Intersection 268", # It doesn't seem to be anIntersection
    "Intersection 36", # It doesn't seem to be anIntersection
    "Intersection 197", # It doesn't seem to be anIntersection
    "Intersection 269", # It doesn't seem to be anIntersection
    "Intersection 135", # It doesn't seem to be anIntersection


}

MANUAL_MOVE_INTERSECTIONS = {
    # name: (x_offset_m, y_offset_m)
    "Intersection 70": (5, -45),
    "Intersection 161": (-35, 25),
    "Intersection 75": (50, 25),
    "Intersection 32": (5, -10),
    "Intersection 107": (-100, -80),
    "Intersection 278": (30, 0), # should change the polygon shape, but moving it is a temporary fix
    "Intersection 242": (-30, 0),
    "Intersection 131": (40, 0),
    "Intersection 197": (-60, 40),
    "Intersection 1": (10, 40),
    "Intersection 86": (20, -30),
    "Intersection 77": (-20, 10),
    "Intersection 78": (-20, -70),
    "Intersection 155": (10, -60),
    "Intersection 168": (-20, 60),
    "Intersection 30": (30, 50),
    "Intersection 150": (880, -170),
    "Intersection 208": (170, 0),
    "Intersection 326": (80, -110),
    "Intersection 301": (-110, 20),
    "Intersection 96": (-30, -10),
    "Intersection 114": (-70, -10),
    "Intersection 283": (-170, 80),
    "Intersection 14": (5, -25),
    "Intersection 272": (250, 200),
    "Intersection 81": (-50, 20),
    "Intersection 11": (100, -50),
    "Intersection 27": (-700, -600),
    "Intersection 220": (100, 10),
    "Intersection 340": (300, -350),
    "Intersection 275": (645, 340),
    "Intersection 90": (-70, 10),
    "Intersection 109": (-20, -10), 
    "Intersection 222": (0, 50), 
    "Intersection 58": (-40, 20), 
    "Intersection 44": (-20, -30), 
    "Intersection 89": (10, 10), 
    "Intersection 48": (20, -10), 
    "Intersection 263": (-30, 0), 
    "Intersection 47": (30, -10), 
    "Intersection 6": (0, 60), 
    "Intersection 8": (-90, 0), 
    "Intersection 216": (30, -10), 
    "Intersection 156": (-20, -30), 
    "Intersection 83": (0, 20), 
    "Intersection 219": (0, -10), 
    "Intersection 218": (0, -10),
}

# -----------------------------
# SWAPIntersectionS
# -----------------------------
# Swap the geometry of two detectedIntersections.
MANUAL_SWAP_INTERSECTIONS = [
    # (
    #     "Intersection 70",
    #     "Intersection 161",
    # ),
]

# -----------------------------
# COPYIntersectionS
# -----------------------------
# Copy an existing polygon and create a newIntersection.
#
# offset_xy is in SOURCE_CRS metres.
MANUAL_COPY_INTERSECTIONS = [
    {
        "SOURCE": "Intersection 241",
        "NEW_NAME": "Intersection 500",
        "OFFSET_XY": (380, -80),
    },
    {
        "SOURCE": "Intersection 242",
        "NEW_NAME": "Intersection 501",
        "OFFSET_XY": (100, -20),
    },
    {
        "SOURCE": "Intersection 213",
        "NEW_NAME": "Intersection 502",
        "OFFSET_XY": (760, 150),
    },
    {
        "SOURCE": "Intersection 96",
        "NEW_NAME": "Intersection 503",
        "OFFSET_XY": (100, 20),
    },
    
    {
        "SOURCE": "Intersection 96",
        "NEW_NAME": "Intersection 504",
        "OFFSET_XY": (180, 34),
    },
    {
        "SOURCE": "Intersection 26",
        "NEW_NAME": "Intersection 505",
        "OFFSET_XY": (605, -250),
    },
    {
        "SOURCE": "Intersection 26",
        "NEW_NAME": "Intersection 506",
        "OFFSET_XY": (530, -220),
    },
    {
        "SOURCE": "Intersection 114",
        "NEW_NAME": "Intersection 507",
        "OFFSET_XY": (140, 0),
    },
    {
        "SOURCE": "Intersection 283",
        "NEW_NAME": "Intersection 508",
        "OFFSET_XY": (-160, 100),
    },
    {
        "SOURCE": "Intersection 14",
        "NEW_NAME": "Intersection 509",
        "OFFSET_XY": (-10, 270),
    },
    {
        "SOURCE": "Intersection 76",
        "NEW_NAME": "Intersection 510",
        "OFFSET_XY": (-10, -100),
    },
    {
        "SOURCE": "Intersection 90",
        "NEW_NAME": "Intersection 511",
        "OFFSET_XY": (50, -150),
    },
    {
        "SOURCE": "Intersection 220",
        "NEW_NAME": "Intersection 512",
        "OFFSET_XY": (150, 0),
    },
    {
        "SOURCE": "Intersection 220",
        "NEW_NAME": "Intersection 513",
        "OFFSET_XY": (310, 0),
    },
    {
        "SOURCE": "Intersection 90",
        "NEW_NAME": "Intersection 514",
        "OFFSET_XY": (100, 0),
    },
    {
        "SOURCE": "Intersection 58",
        "NEW_NAME": "Intersection 515",
        "OFFSET_XY": (95, -80),
    },
    {
        "SOURCE": "Intersection 44",
        "NEW_NAME": "Intersection 516",
        "OFFSET_XY": (80, 100),
    },
    {
        "SOURCE": "Intersection 99",
        "NEW_NAME": "Intersection 517",
        "OFFSET_XY": (120, 180),
    },
    {
        "SOURCE": "Intersection 180",
        "NEW_NAME": "Intersection 518",
        "OFFSET_XY": (-150, 40),
    },
    {
        "SOURCE": "Intersection 99",
        "NEW_NAME": "Intersection 519",
        "OFFSET_XY": (480, -150),
    },
    {
        "SOURCE": "Intersection 208",
        "NEW_NAME": "Intersection 520",
        "OFFSET_XY": (400, -620),
    },
    {
        "SOURCE": "Intersection 150",
        "NEW_NAME": "Intersection 521",
        "OFFSET_XY": (-600, -400),
    },
    {
        "SOURCE": "Intersection 156",
        "NEW_NAME": "Intersection 522",
        "OFFSET_XY": (120, 0),
    },
]

# Optional: replace a detected polygon completely with your own manually drawn polygon.
# Coordinates must be SOURCE_CRS metres.
# This is useful when moving is not enough and the shape itself is wrong.
MANUAL_REPLACE_INTERSECTION_POLYGONS = {
    # "Intersection 70": Polygon([
    #     (482000, 7513000),
    #     (482050, 7513000),
    #     (482050, 7513050),
    #     (482000, 7513050),
    #     (482000, 7513000),
    # ]),
}

# Optional: add completely newIntersections that were missed by the detector.
# Coordinates must be SOURCE_CRS metres.
MANUAL_ADD_INTERSECTION_POLYGONS = [
    # (
    #     "ManualIntersection A",
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
    "Intersection 242",
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
# DETECTIntersectionS (HYBRID)
# -----------------------------
def detect_intersections_hybrid(lane_gdf):
    # _outputs = detect_intersections_with__package(DXF_PATH)
    # _intersections = _outputs["intersections"]
    _intersections = detect_intersections_from_dxf_lanes(lane_gdf)
    fallback_intersections = empty_intersection_gdf()

    combined = pd.concat(
        [_intersections, fallback_intersections],
        ignore_index=True,
    )

    if combined.empty:
        return _intersections

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
            "INTERSECTION_NAME": f"HybridIntersection {len(final_records) + 1}",
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
#  / GRAPHIntersection DETECTOR
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


def _generate_s_from_road_polygons(
    road_polygon_gdf,
    interpolation_distance=_INTERPOLATION_DISTANCE,
):
    if  is None:
        raise ImportError(
            "The  package is not installed. Install it with: "
            "pip install  networkx"
        )

    records = []

    for _, row in road_polygon_gdf.iterrows():
        polygon = row.geometry

        if polygon is None or polygon.is_empty:
            continue

        try:
            centreline_obj = (
                polygon,
                interpolation_distance=interpolation_distance,
            )
            centreline_geom = centreline_obj.geometry

        except Exception as exc:
            debug_log(
                f" failed for road polygon {row.get('ROAD_POLYGON_ID', '-')}",
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


def _build__graph(_gdf):
    graph = nx.Graph()
    segment_records = []

    for _, row in _gdf.iterrows():
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


def detect_intersections_from_s(
    _segments_gdf,
    graph,
    min_graph_degree=MIN_GRAPH_DEGREE,
    cluster_buffer_m=INTERSECTION_CLUSTER_BUFFER_METRES,
    polygon_radius_m=INTERSECTION_POLYGON_RADIUS_METRES,
    polygon_buffer_m=INTERSECTION_POLYGON_BUFFER_METRES,
):
    if _segments_gdf.empty or graph.number_of_nodes() == 0:
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

        nearby_segments = _segments_gdf[
            _segments_gdf.geometry.intersects(local_area)
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

      Intersection_polygon = (
            nearby_segments.geometry
            .intersection(local_area)
            .buffer(polygon_buffer_m, cap_style=2, join_style=2)
            .union_all()
            .convex_hull
            .buffer(0)
        )

        ifIntersection_polygon.is_empty:
            continue

        marker_point =Intersection_polygon.centroid

        records.append({
            "INTERSECTION_NAME": f"Intersection {len(records) + 1}",
            "INTERSECTION_X": marker_point.x,
            "INTERSECTION_Y": marker_point.y,
            "GRAPH_NODE_COUNT": int(graph_node_count),
            "MAX_GRAPH_DEGREE": int(max_graph_degree),
            "BRANCH_COUNT": int(branch_count),
            "DIRECTION_GROUP_COUNT": int(direction_group_count),
            "geometry":Intersection_polygon,
        })

    if not records:
        debug_log("No centreline graphIntersection clusters passed thresholds")
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
        f"Intersection {idx + 1}"
        for idx in range(len(result))
    ]

    result["DETECTION_METHOD"] = "_GRAPH"

    debug_log(
        "Detected centreline graphIntersections",
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
    DetectIntersections by converting lane LineStrings into road polygons,
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
    _gdf = _generate_s_from_road_polygons(road_polygon_gdf)
    debug_log("Generated centrelines", _gdf)

    if _gdf.empty:
        return empty_intersection_gdf()

    debug_log("Building centreline graph")
    graph, _segments_gdf = _build__graph(_gdf)
    debug_log("Graph node/edge count", {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
    })

    return detect_intersections_from_s(
        _segments_gdf=_segments_gdf,
        graph=graph,
    )


def export_intersections_csv(intersection_gdf, output_path="detected__intersections.csv"):
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

    ifIntersection_gdf.empty:
        pd.DataFrame(columns=columns).to_csv(output_path, index=False)
        return

    export_df =Intersection_gdf.copy()
    export_df["geometry_wkt"] = export_df.geometry.to_wkt()

    for column in columns:
        if column not in export_df.columns:
            export_df[column] = None

    export_df[columns].to_csv(output_path, index=False)



def apply_manual_intersection_edits(intersection_gdf):
    """
    Apply manual DELETE / MOVE / REPLACE / ADD rules to detectedIntersections.

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

    ifIntersection_gdf is None orIntersection_gdf.empty:
        edited = gpd.GeoDataFrame(
            {column: [] for column in expected_columns},
            geometry=[],
            crs=TARGET_CRS,
        )
    else:
        edited =Intersection_gdf.copy()
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
        forIntersection_name, replacement_polygon in MANUAL_REPLACE_INTERSECTION_POLYGONS.items():
            mask = edited["INTERSECTION_NAME"].astype(str) ==Intersection_name

            if not mask.any():
                continue

            edited.loc[mask, "geometry"] = replacement_polygon
            edited.loc[mask, "MANUAL_EDIT"] = "REPLACE"
            edited.loc[mask, "DETECTION_METHOD"] = (
                edited.loc[mask, "DETECTION_METHOD"].fillna("AUTO").astype(str)
                + "+MANUAL_REPLACE"
            )
        # PRINT: print polygons for manual inspection.
        forIntersection_name in PRINT_INTERSECTIONS_POLYGONS:
            mask = edited["INTERSECTION_NAME"].astype(str) ==Intersection_name

            if not mask.any():
                continue

            print(edited.loc[mask, ["INTERSECTION_NAME", "geometry"]].to_wkt())
            print(json.dumps(json.loads(edited.loc[mask, "geometry"].to_json()), indent=2))
        # MOVE: shift polygon by x/y metres.
        forIntersection_name, offsets in MANUAL_MOVE_INTERSECTIONS.items():
            x_offset_m, y_offset_m = offsets
            mask = edited["INTERSECTION_NAME"].astype(str) ==Intersection_name

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

    # ADD: add missedIntersections manually.
    add_records = []

    forIntersection_name, polygon in MANUAL_ADD_INTERSECTION_POLYGONS:
        add_records.append({
            "INTERSECTION_NAME":Intersection_name,
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
    # COPY existingIntersection
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
        "ManualIntersection edits applied",
        edited[[
            "INTERSECTION_NAME",
            "DETECTION_METHOD",
            "MANUAL_EDIT",
            "INTERSECTION_X",
            "INTERSECTION_Y",
        ]],
    )

    return edited.to_crs(TARGET_CRS)

def point_is_in_intersection_area(point_source,Intersection_gdf):
    """Return True when a projected point is inside or within tolerance of anIntersection."""
    if point_source is None or point_source.is_empty orIntersection_gdf.empty:
        return False

  Intersections_source =Intersection_gdf.to_crs(SOURCE_CRS)

    return (
      Intersections_source.geometry.contains(point_source).any()
        or (
          Intersections_source.geometry.distance(point_source)
            <=Intersection_POINT_TOLERANCE_METRES
        ).any()
    )


# -----------------------------
# GPKG ForIntersections
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

      Intersection_poly = (
            nearby_lanes.geometry
            .buffer(8)
            .union_all()
            .convex_hull
        )

        candidate_polygons.append(intersection_poly)
        names.append(f"AutoIntersection {idx + 1}")

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
            user = "FATEMEH.HAERI@FORTESCUE.COM",
            password = "",
            account = "FMG-WN74261",
            database = "AA_OPERATIONS_MANAGEMENT",
            schema = "SELFSERVICE",
            warehouse = "WH_AA_OPERATIONS_MANAGEMENT",
            role = "EDW_FATEMEH.HAERI"
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

    for name, polygon inIntersection_POLYGONS:
        names.append(name)
        polygons.append(polygon)

    return gpd.GeoDataFrame(
        {"INTERSECTION_NAME": names},
        geometry=polygons,
        crs=SOURCE_CRS
    ).to_crs(TARGET_CRS)


def classify_area_categories(gdf, lane_gdf,Intersection_gdf):
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

    ifIntersection_gdf is not None and notIntersection_gdf.empty:
      Intersections_source =Intersection_gdf.to_crs(SOURCE_CRS)[
            ["INTERSECTION_NAME", "geometry"]
        ].copy()
        # Buffer once, then use a spatial-indexed join for contains/near tolerance.
      Intersections_source["geometry"] =Intersections_source.geometry.buffer(
          Intersection_POINT_TOLERANCE_METRES
        )
        hits = gpd.sjoin(
            points_source[["_ROW_ID", "geometry"]],
          Intersections_source,
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

    classified = classify_area_categories(gdf_source, dxf_gdf,Intersection_gdf)
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
  Intersection_gdf,
    geometry="geometry",
    crs=TARGET_CRS
)

# Apply manual corrections after automatic detection and manual base polygons.
# This lets you remove false positives, move polygons, replace polygons,
# or add missedIntersections from the CONFIG section above.
intersection_gdf = apply_manual_intersection_edits(intersection_gdf)
export_intersections_csv(intersection_gdf, "final_intersections_after_manual_edits.csv")

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

for _, row inIntersection_gdf.iterrows():
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

    centre = polygon.centroid
  Intersection_name = row.get("INTERSECTION_NAME", "-")

    static_layers.append(
        dl.DivMarker(
            position=[centre.y, centre.x],
            iconOptions={
                "html": (
                    "<div style='"
                    "background: orange;"
                    "color: black;"
                    "font-size: 11px;"
                    "font-weight: bold;"
                    "padding: 2px 5px;"
                    "border: 1px solid black;"
                    "border-radius: 4px;"
                    "white-space: nowrap;"
                    "'>"
                    f"{intersection_name}"
                    "</div>"
                ),
                "className": "intersection-label",
                "iconSize": [120, 22],
                "iconAnchor": [60, 11],
            },
        )
    )


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
    prefix_colors = {
            "DZ": "blue",
            "WD": "green",
            "WC": "pink",
            "GR": "brown",
            "LV": "red",
            "EL": "red",
            "WL": "purple",
            "EX": "black"
        }
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

    near_miss_points = gdf_plot[
        (gdf_plot["INTERACTION_TYPE"] == "NEAR_MISS") &
        (gdf_plot["DISTANCE_METRES"] < 15)
    ].dropna(subset=["lon", "lat"]).sort_values("DISTANCE_METRES").groupby(["MACHINE", "PAIR_MACHINE"], as_index=False).first().copy()

    # ----------------------------------------------
    # Identify first HT points within CAS range for each HT-HOV pair to mark on the map
    # ----------------------------------------------
    ht_points = (
    gdf_plot[(gdf_plot["TYPE"] == "HT")]
        .dropna(subset=["lon", "lat"])
        .sort_values(["MACHINE", "PAIR_MACHINE", "TIME_INT"])
        .copy()
    )
    # New CAS interaction starts when same HT/HOV pair has a time gap

    MAX_GAP_SECONDS = 5

    ht_points["PREV_TIME_INT"] = (
        ht_points
        .groupby(["MACHINE", "PAIR_MACHINE"])["TIME_INT"]
        .shift()
    )

    ht_points["NEW_INTERACTION"] = (
        ht_points["PREV_TIME_INT"].isna() |
        ((ht_points["TIME_INT"] - ht_points["PREV_TIME_INT"]) > MAX_GAP_SECONDS)
    )

    ht_points["INTERACTION_ID"] = (
        ht_points
        .groupby(["MACHINE", "PAIR_MACHINE"])["NEW_INTERACTION"]
        .cumsum()
    )

    first_ht_points = (
        ht_points
        .sort_values(["MACHINE", "PAIR_MACHINE", "INTERACTION_ID", "TIME_INT"])
        .drop_duplicates(
            subset=["MACHINE", "PAIR_MACHINE", "INTERACTION_ID"],
            keep="first"
        )
    )

    print(len(first_ht_points))

    # ----------------------------------------------
    # Identify first HOV points within CAS range for each HT-HOV pair to mark on the map
    # ----------------------------------------------
    """
    hov_points = (
    gdf_plot[(gdf_plot["TYPE"] == "HOV")]
        .dropna(subset=["lon", "lat"])
        .sort_values(["MACHINE", "PAIR_MACHINE", "TIME_INT"])
        .copy()
    )
    # New CAS interaction starts when same HT/HOV pair has a time gap

    MAX_GAP_SECONDS = 5

    hov_points["PREV_TIME_INT"] = (
        hov_points
        .groupby(["MACHINE", "PAIR_MACHINE"])["TIME_INT"]
        .shift()
    )

    hov_points["NEW_INTERACTION"] = (
        hov_points["PREV_TIME_INT"].isna() |
        ((hov_points["TIME_INT"] - hov_points["PREV_TIME_INT"]) > MAX_GAP_SECONDS)
    )

    hov_points["INTERACTION_ID"] = (
        hov_points
        .groupby(["MACHINE", "PAIR_MACHINE"])["NEW_INTERACTION"]
        .cumsum()
    )

    first_hov_points = (
        hov_points
        .sort_values(["MACHINE", "PAIR_MACHINE", "INTERACTION_ID", "TIME_INT"])
        .drop_duplicates(
            subset=["MACHINE", "PAIR_MACHINE", "INTERACTION_ID"],
            keep="first"
        )
    )
    """
    if gdf_plot.empty:
        return []

    layers = []

    # def build_near_miss_marker(row):
    #     marker_label = "!" if row["TYPE"] == "HT" else "⚠"
    #     return dl.DivMarker(
    #         id=(
    #             f"NEAR_MISS_{row['TYPE']}_"
    #             f"{row['MACHINE']}_"
    #             f"{row['PAIR_MACHINE']}_"
    #             f"{row['TIME_INT']}_"
    #             f"{row['lat']:.6f}_"
    #             f"{row['lon']:.6f}"
    #         ),
    #         position=[row["lat"], row["lon"]],
    #         iconOptions={
    #             "html": (
    #                 "<div style='font-size:18px;font-weight:bold;"
    #                 "color:red;background:white;border:2px solid red;"
    #                 "border-radius:50%;width:22px;height:22px;"
    #                 "line-height:20px;text-align:center;'>"
    #                 f"{marker_label}</div>"
    #             ),
    #             "className": "near-miss-machine-marker",
    #             "iconSize": [22, 22],
    #             "iconAnchor": [11, 11],
    #         },
    #         children=[
    #             dl.Tooltip([
    #                 html.Div("Near miss: distance < 15 m"),
    #                 html.Div(f"Type: {row['TYPE']}"),
    #                 html.Div(f"Machine: {row['MACHINE']}"),
    #                 html.Div(f"Near Machine: {row['PAIR_MACHINE']}"),
    #                 html.Div(f"Time: {row['TIMESTAMP']}"),
    #                 html.Div(f"Distance: {row['DISTANCE_METRES']:.2f} m"),
    #                 html.Div(f"Moving: {row['MOVING_STATUS']}"),
    #                 html.Div(f"Area: {row['AREA_CATEGORY']}"),
    #                 html.Div(f"X: {row['X']:.2f}"),
    #                 html.Div(f"Y: {row['Y']:.2f}"),
    #             ])
    #         ],
    #     )

    # for near_miss_row in near_miss_points.to_dict("records"):
    #     layers.append(build_near_miss_marker(near_miss_row))

    # for _,Intersection_row inIntersection_points.iterrows():
    #     layers.append(
    #         dl.CircleMarker(
    #             id=(
    #                 f"INTERSECTION_POINT_"
    #                 f"{intersection_row['TYPE']}_"
    #                 f"{intersection_row['MACHINE']}_"
    #                 f"{intersection_row['PAIR_MACHINE']}_"
    #                 f"{intersection_row['TIME_INT']}_"
    #                 f"{intersection_row['lat']:.6f}_"
    #                 f"{intersection_row['lon']:.6f}"
    #             ),
    #             center=[intersection_row["lat"],Intersection_row["lon"]],
    #             radius=9,
    #             color="orange",
    #             fillColor="orange",
    #             fill=True,
    #             fillOpacity=0.35,
    #             weight=3,
    #             children=[
    #                 dl.Tooltip([
    #                     html.Div("Point inside calculatedIntersection area"),
    #                     html.Div(f"Intersection: {intersection_row['INTERSECTION_NAME'] or '-'}"),
    #                     html.Div(f"Type: {intersection_row['TYPE']}"),
    #                     html.Div(f"Machine: {intersection_row['MACHINE']}"),
    #                     html.Div(f"Near Machine: {intersection_row['PAIR_MACHINE']}"),
    #                     html.Div(f"Time: {intersection_row['TIMESTAMP']}"),
    #                     html.Div(f"Distance to lane: {intersection_row['DISTANCE_TO_LANE_M']:.2f} m"),
    #                     html.Div(f"X: {intersection_row['X']:.2f}"),
    #                     html.Div(f"Y: {intersection_row['Y']:.2f}")
    #                 ])
    #             ]
    #         )
    #     )

    grouped = {
        (machine_type, prefix): group.sort_values("TIME_INT")
        for (machine_type, prefix), group in gdf_plot.groupby(["TYPE", "PREFIX"])
    }

    for _, group in grouped.items():
        group = group.dropna(subset=["lon", "lat"])

        if group.empty:
            continue

        for _, row in group.iterrows():
            if row["TYPE"] == "HT":
                marker_color = "yellow"
            else:
                marker_color = prefix_colors.get(row["PREFIX"], "gray")

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
                            html.Div(f"Area: {row['AREA_CATEGORY']}"),
                            html.Div(f"Intersection: {row['INTERSECTION_NAME'] or '-'}"),
                            html.Div(f"Distance to lane: {row['DISTANCE_TO_LANE_M']:.2f} m"),
                            html.Div(f"X: {row['X']:.2f}"),
                            html.Div(f"Y: {row['Y']:.2f}")
                        ])
                    ]
                )
            )

    for _, value in first_ht_points.iterrows():
        layers.append(
            dl.DivMarker(
                id=(
                    f"FIRST_HT_DT_X_"
                    f"{value['MACHINE']}_"
                    f"{value['PAIR_MACHINE']}_"
                    f"{value['TIME_INT']}"
                ),
                position=[value["lat"], value["lon"]],
                iconOptions={
                    "html": "<div style='font-size:22px;font-weight:bold;color:black;text-shadow:0 0 3px white;'>X</div>",
                    "className": "ht-dt-entry-x-marker",
                    "iconSize": [22, 22]
                },
                children=[
                    dl.Tooltip([
                        html.Div("First HT/DT point in interaction"),
                        html.Div(f"HT/DT: {value['MACHINE']}"),
                        html.Div(f"HOV: {value['PAIR_MACHINE']}"),
                        html.Div(f"Time: {value['TIMESTAMP']}"),
                        html.Div(f"Distance: {value['DISTANCE_METRES']:.2f} m"),
                        html.Div(f"Interaction: {value['INTERACTION_TYPE']}"),
                        html.Div(f"Moving: {value['MOVING_STATUS']}"),
                        html.Div(f"Area: {value['AREA_CATEGORY']}"),
                        html.Div(f"X: {value['X']:.2f}"),
                        html.Div(f"Y: {value['Y']:.2f}")
                    ])
                ]
            )
        )
    """
    for _, value in first_hov_points.iterrows():
        layers.append(
            dl.DivMarker(
                id=(
                    f"FIRST_HOV_X_"
                    f"{value['MACHINE']}_"
                    f"{value['PAIR_MACHINE']}_"
                    f"{value['TIME_INT']}"
                ),
                position=[value["lat"], value["lon"]],
                iconOptions={
                    "html": "<div style='font-size:22px;font-weight:bold;color:red;text-shadow:0 0 3px white;'>X</div>",
                    "className": "hov-entry-x-marker",
                    "iconSize": [22, 22]
                },
                children=[
                    dl.Tooltip([
                        html.Div("First HOV point in interaction"),
                        html.Div(f"HOV: {value['MACHINE']}"),
                        html.Div(f"HT/DT: {value['PAIR_MACHINE']}"),
                        html.Div(f"Time: {value['TIMESTAMP']}"),
                        html.Div(f"Distance: {value['DISTANCE_METRES']:.2f} m"),
                        html.Div(f"Interaction: {value['INTERACTION_TYPE']}"),
                        html.Div(f"Moving: {value['MOVING_STATUS']}"),
                        html.Div(f"Area: {value['AREA_CATEGORY']}"),
                        html.Div(f"X: {value['X']:.2f}"),
                        html.Div(f"Y: {value['Y']:.2f}")
                    ])
                ]
            )
        )
    """
    return layers


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, port=8002)