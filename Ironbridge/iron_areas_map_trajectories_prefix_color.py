import pandas as pd
import geopandas as gpd
from shapely import wkt
import snowflake.connector
from sqlalchemy import create_engine
import ezdxf
import math
import numpy as np
import traceback
from datetime import datetime
import networkx as nx
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union, nearest_points
from shapely.affinity import translate
from lxml import etree
import os
import json
from functools import lru_cache
from dash import Dash, dcc, html, no_update
from dash.dependencies import Input, Output, State
import dash_leaflet as dl

try:
    from centerline.geometry import Centerline
except ImportError:
    Centerline = None


# -----------------------------
# CONFIG
# -----------------------------
DXF_PATH = r"Ironbridge/Layers/Ironbridge_dxf.dxf"
KML_FILE = r"Ironbridge/Layers/Ironbridge_Layer.kml"
GPKG_PATH = r"Ironbridge/Layers/Ironbridge_package.gpkg"
WMS_URL = "https://fortescuesky.fmgl.com.au/SG/default/streamer.ashx?"

SOURCE_CRS = "EPSG:28350"
TARGET_CRS = "EPSG:4326"

# DEFAULT_START_DATETIME = "2026-05-09 11:19:01"
# DEFAULT_END_DATETIME = "2026-05-09 13:19:01"

# DEFAULT_START_DATETIME = "2026-07-16 11:00:00"
# DEFAULT_END_DATETIME = "2026-07-16 13:00:00"

DEFAULT_START_DATETIME = "2026-06-15 11:00:00"
DEFAULT_END_DATETIME = "2026-06-15 11:30:00"


HOV_ASSET_TYPES = [
    "DZ",
    "RL",
    "IB",
    "LV",
    "EX",
    "CC",
    "GR",
    "LO",
]

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

PREFIX_COLORS = {
    "RD": "#1f77b4",   # Road train / haul truck
    "DZ": "#2ca02c",   # Dozer
    "RL": "#9467bd",
    "IB": "#8c564b",
    "LV": "#d62728",
    "ELI": "#e377c2",
    "EX": "#000000",   # Excavator
    "CC": "#17becf",
    "GR": "#bcbd22",
    "LO": "#ff7f0e",
}

DEFAULT_PREFIX_COLOR = "#7f7f7f"
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



def clean_datetime(value, default_value):
    if value is None or str(value).strip() == "":
        return default_value
    return str(value).strip()


def empty_geodataframe(crs, columns=None):
    data = {column: pd.Series(dtype="object") for column in (columns or [])}
    return gpd.GeoDataFrame(
        data,
        geometry=gpd.GeoSeries([], crs=crs),
        crs=crs,
    )


def ensure_geodataframe(value, crs, name="GeoDataFrame"):
    if value is None:
        return empty_geodataframe(crs)

    frame = value.copy() if isinstance(value, gpd.GeoDataFrame) else pd.DataFrame(value).copy()

    if "geometry" not in frame.columns:
        if len(frame) == 0:
            return empty_geodataframe(crs, list(frame.columns))
        raise ValueError(
            f"{name} does not contain a geometry column. "
            f"Columns: {list(frame.columns)}"
        )

    return gpd.GeoDataFrame(
        frame,
        geometry="geometry",
        crs=getattr(value, "crs", None) or crs,
    )


def concat_geodataframes(frames, crs):
    valid_frames = []

    for frame in frames:
        if frame is None:
            continue

        frame = ensure_geodataframe(frame, crs, "GeoDataFrame being concatenated")

        if frame.empty:
            continue

        if frame.crs != crs:
            frame = frame.to_crs(crs)

        valid_frames.append(frame)

    if not valid_frames:
        return empty_intersection_gdf(crs)

    table = pd.concat(
        [pd.DataFrame(frame.copy()) for frame in valid_frames],
        ignore_index=True,
        sort=False,
    )

    return gpd.GeoDataFrame(
        table,
        geometry="geometry",
        crs=crs,
    )


# Define intersection boxes in SOURCE_CRS metres: (name, min_x, min_y, max_x, max_y).
# Add/edit boxes after visually checking the X markers against the map.
INTERSECTION_BOXES = [
    # ("Intersection 1", 650000, 7550000, 650100, 7550100),
]
INTERSECTION_POLYGONS = [
    # (
    #     "Intersection 1",
    #     Polygon([
    #         (482120.50207447, 7513691.76620729),
    #         (482045.53212524, 7513702.21283956),
    #         (482048.60466414, 7513848.46569136),
    #         (482037.54352409, 7513865.05740143),
    #         (482072.57046758, 7513920.97760947),
    #         (482214.52176491, 7513850.92372248),
    #         (482193.62850037, 7513686.85014505),
    #         (482120.50207447, 7513691.76620729),
    #     ]),
        
    # )
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
    "Centerline Intersection 9",
    "Centerline Intersection 86",
    "Centerline Intersection 153",
    "Centerline Intersection 103",
    "Centerline Intersection 121",
    "Centerline Intersection 84",
    "Centerline Intersection 4",
    "Centerline Intersection 130",
    "Centerline Intersection 151",
    "Centerline Intersection 115",
    "Centerline Intersection 26",
    "Centerline Intersection 50",
    "Centerline Intersection 131",
    "Centerline Intersection 126",
    "Centerline Intersection 57",
    "Centerline Intersection 149",
    "Centerline Intersection 146",
    "Centerline Intersection 127",
    "Centerline Intersection 42",
    "Centerline Intersection 53",
    "Centerline Intersection 68",
    "Centerline Intersection 148",
    "Centerline Intersection 134",
    "Centerline Intersection 70",
    "Centerline Intersection 77",
    "Centerline Intersection 74",
    "Centerline Intersection 127",
    "Centerline Intersection 146",
    "Centerline Intersection 149",
    "Centerline Intersection 32",
    "Centerline Intersection 108",
    "Centerline Intersection 145",
    "Centerline Intersection 58",
    "Centerline Intersection 71",
    "Centerline Intersection 142",
    "Centerline Intersection 47",
    "Centerline Intersection 41",
    "Centerline Intersection 16",
    "Centerline Intersection 120",
    "Centerline Intersection 59",
    "Centerline Intersection 69",
    "Centerline Intersection 98",
    "Centerline Intersection 114",
    "Centerline Intersection 104",
    "Centerline Intersection 143",
    "Centerline Intersection 90",
    "Centerline Intersection 30",
    "Centerline Intersection 38",
    "Centerline Intersection 155",
    "Centerline Intersection 144",
    "Centerline Intersection 48",
    "Centerline Intersection 15",
    "Centerline Intersection 36",
    "Centerline Intersection 122",
    "Centerline Intersection 89",
    "Centerline Intersection 125",
    "Centerline Intersection 51",
    "Centerline Intersection 147",
    "Centerline Intersection 136",
    "Centerline Intersection 112",
    "Centerline Intersection 81",
    "Centerline Intersection 154",
    "Centerline Intersection 106",
    "Centerline Intersection 76",
    "Centerline Intersection 118",
    "Centerline Intersection 132",
    "Centerline Intersection 18",
    "Centerline Intersection 12",
    "Centerline Intersection 83",
    "Centerline Intersection 87",
    "Centerline Intersection 66",
    "Centerline Intersection 65",
    "Centerline Intersection 150",
    "Centerline Intersection 22",
    "Centerline Intersection 29",
    "Centerline Intersection 37",
    "Centerline Intersection 19",
    "Centerline Intersection 105",
    "Centerline Intersection 133",
    "Centerline Intersection 119",
    "Centerline Intersection 75",
    "Centerline Intersection 116",
    "Centerline Intersection 123",
    "Centerline Intersection 129",
    "Centerline Intersection 116",
    "Centerline Intersection 85",
    "Centerline Intersection 135",
    "Centerline Intersection 72",
    "Centerline Intersection 141",
    "Centerline Intersection 3",
    "Centerline Intersection 64",
    "Centerline Intersection 28",
    "Centerline Intersection 73",
    "Centerline Intersection 152",
    "Centerline Intersection 124",
    "Centerline Intersection 128",


}

MANUAL_MOVE_INTERSECTIONS = {
    # name: (x_offset_m, y_offset_m)
    "Centerline Intersection 8": (-30, 320),
    "Centerline Intersection 91": (30, -40),
    "Centerline Intersection 25": (20, 0),
    "Centerline Intersection 54": (-30, 0),
    "Centerline Intersection 56": (-10, -30),
    "Centerline Intersection 34": (-70, -10),
    "Centerline Intersection 7": (50, 10),
    "Centerline Intersection 137": (0, -30),
    "Centerline Intersection 49": (50, 0),
    "Centerline Intersection 40": (-20, 220),
    "Centerline Intersection 10": (0, 20),
    "Centerline Intersection 27": (0, -30),
    "Centerline Intersection 24": (20, 50),
    "Centerline Intersection 82": (20, -20),
    "Centerline Intersection 97": (20, -10),
    "Centerline Intersection 113": (0, 10),
    "Centerline Intersection 11": (90, 360),
    "Centerline Intersection 35": (0, -100),
    "Centerline Intersection 100": (-20, 20),
    "Centerline Intersection 6": (20, 30),
    "Centerline Intersection 63": (-20, 30),
    "Centerline Intersection 78": (40, -60),
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
        "SOURCE": "Centerline Intersection 10",
        "NEW_NAME": "Centerline Intersection 500",
        "OFFSET_XY": (-10, -50),
    },
    # {
    #     "SOURCE": "Centerline Intersection 242",
    #     "NEW_NAME": "Centerline Intersection 501",
    #     "OFFSET_XY": (100, -20),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 213",
    #     "NEW_NAME": "Centerline Intersection 502",
    #     "OFFSET_XY": (760, 150),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 96",
    #     "NEW_NAME": "Centerline Intersection 503",
    #     "OFFSET_XY": (100, 20),
    # },
    
    # {
    #     "SOURCE": "Centerline Intersection 96",
    #     "NEW_NAME": "Centerline Intersection 504",
    #     "OFFSET_XY": (180, 34),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 26",
    #     "NEW_NAME": "Centerline Intersection 505",
    #     "OFFSET_XY": (605, -250),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 26",
    #     "NEW_NAME": "Centerline Intersection 506",
    #     "OFFSET_XY": (530, -220),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 114",
    #     "NEW_NAME": "Centerline Intersection 507",
    #     "OFFSET_XY": (140, 0),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 283",
    #     "NEW_NAME": "Centerline Intersection 508",
    #     "OFFSET_XY": (-160, 100),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 14",
    #     "NEW_NAME": "Centerline Intersection 509",
    #     "OFFSET_XY": (-10, 270),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 76",
    #     "NEW_NAME": "Centerline Intersection 510",
    #     "OFFSET_XY": (-10, -100),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 90",
    #     "NEW_NAME": "Centerline Intersection 511",
    #     "OFFSET_XY": (50, -150),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 220",
    #     "NEW_NAME": "Centerline Intersection 512",
    #     "OFFSET_XY": (150, 0),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 220",
    #     "NEW_NAME": "Centerline Intersection 513",
    #     "OFFSET_XY": (310, 0),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 90",
    #     "NEW_NAME": "Centerline Intersection 514",
    #     "OFFSET_XY": (100, 0),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 58",
    #     "NEW_NAME": "Centerline Intersection 515",
    #     "OFFSET_XY": (95, -80),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 44",
    #     "NEW_NAME": "Centerline Intersection 516",
    #     "OFFSET_XY": (80, 100),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 99",
    #     "NEW_NAME": "Centerline Intersection 517",
    #     "OFFSET_XY": (120, 180),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 180",
    #     "NEW_NAME": "Centerline Intersection 518",
    #     "OFFSET_XY": (-150, 40),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 99",
    #     "NEW_NAME": "Centerline Intersection 519",
    #     "OFFSET_XY": (480, -150),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 208",
    #     "NEW_NAME": "Centerline Intersection 520",
    #     "OFFSET_XY": (400, -620),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 150",
    #     "NEW_NAME": "Centerline Intersection 521",
    #     "OFFSET_XY": (-600, -400),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 156",
    #     "NEW_NAME": "Centerline Intersection 522",
    #     "OFFSET_XY": (120, 0),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 158",
    #     "NEW_NAME": "Centerline Intersection 523",
    #     "OFFSET_XY": (610, -130),
    # },
    # {
    #     "SOURCE": "Centerline Intersection 206",
    #     "NEW_NAME": "Centerline Intersection 524",
    #     "OFFSET_XY": (0, 95),
    # },
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
# DXF LOADER
# -----------------------------
def _extract_dxf_coordinates(entity):
    entity_type = entity.dxftype()

    if entity_type == "LINE":
        return [
            (float(entity.dxf.start.x), float(entity.dxf.start.y)),
            (float(entity.dxf.end.x), float(entity.dxf.end.y)),
        ], False

    if entity_type == "LWPOLYLINE":
        return [
            (float(point[0]), float(point[1]))
            for point in entity.get_points("xy")
        ], bool(entity.closed)

    if entity_type == "POLYLINE":
        return [
            (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
            for vertex in entity.vertices
        ], bool(entity.is_closed)

    return None, False


def load_dxf_lanes(dxf_path):
    if not os.path.exists(dxf_path):
        raise FileNotFoundError(f"DXF file was not found: {dxf_path}")

    document = ezdxf.readfile(dxf_path)
    modelspace = document.modelspace()

    lane_records = []
    fallback_records = []
    available_layers = set()

    for entity in modelspace:
        if entity.dxftype() not in {"LINE", "LWPOLYLINE", "POLYLINE"}:
            continue

        layer_name = str(entity.dxf.layer)
        available_layers.add(layer_name)
        points, is_closed = _extract_dxf_coordinates(entity)

        if not points or len(points) < 2:
            continue

        if is_closed and points[0] != points[-1]:
            points.append(points[0])

        geometry = LineString(points)
        if geometry.is_empty:
            continue

        record = {
            "polyline_id": str(entity.dxf.handle),
            "layer": layer_name,
            "vertex_count": len(points),
            "entity_type": entity.dxftype(),
            "geometry": geometry,
        }
        fallback_records.append(record)

        if "LANE" in layer_name.upper():
            lane_records.append(record)

    records = lane_records if lane_records else fallback_records

    if not records:
        raise ValueError(
            "No usable LINE, LWPOLYLINE or POLYLINE geometry was found "
            f"in the DXF. Available layers: {sorted(available_layers)}"
        )

    if not lane_records:
        debug_log(
            "No DXF layer containing 'LANE' was found. "
            "Using all supported line entities."
        )

    dxf_gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=SOURCE_CRS)
    dxf_gdf = dxf_gdf[
        dxf_gdf["geometry"].notna() & ~dxf_gdf["geometry"].is_empty
    ].copy()

    if dxf_gdf.empty:
        return empty_geodataframe(
            TARGET_CRS,
            ["polyline_id", "layer", "vertex_count", "entity_type"],
        )

    dxf_gdf = dxf_gdf.to_crs(TARGET_CRS)
    dxf_gdf["geometry"] = dxf_gdf.geometry.simplify(
        0.000003, preserve_topology=True
    )

    return gpd.GeoDataFrame(dxf_gdf, geometry="geometry", crs=TARGET_CRS)

# -----------------------------
# DETECT INTERSECTIONS (HYBRID)
# -----------------------------
def detect_intersections_hybrid(lane_gdf):
    return detect_intersections_from_dxf_lanes(lane_gdf)

# -----------------------------
# CENTERLINE / GRAPH INTERSECTION DETECTOR
# -----------------------------
def empty_intersection_gdf(crs=TARGET_CRS):
    return gpd.GeoDataFrame(
        {
            "INTERSECTION_NAME": pd.Series(dtype="object"),
            "INTERSECTION_X": pd.Series(dtype="float64"),
            "INTERSECTION_Y": pd.Series(dtype="float64"),
            "GRAPH_NODE_COUNT": pd.Series(dtype="float64"),
            "MAX_GRAPH_DEGREE": pd.Series(dtype="float64"),
            "BRANCH_COUNT": pd.Series(dtype="float64"),
            "DIRECTION_GROUP_COUNT": pd.Series(dtype="float64"),
            "DETECTION_METHOD": pd.Series(dtype="object"),
            "MANUAL_EDIT": pd.Series(dtype="object"),
        },
        geometry=gpd.GeoSeries([], crs=crs),
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

    if not segment_records:
        return graph, gpd.GeoDataFrame(
            {
                "node_a": pd.Series(dtype="object"),
                "node_b": pd.Series(dtype="object"),
                "direction_bin": pd.Series(dtype="float64"),
            },
            geometry=gpd.GeoSeries([], crs=SOURCE_CRS),
            crs=SOURCE_CRS,
        )

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
        edited = concat_geodataframes([edited, add_gdf], crs=SOURCE_CRS)

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

        edited = concat_geodataframes([edited, copied_gdf], crs=SOURCE_CRS)
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
    tree = etree.parse(str(kml_file))

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

    hov_type_sql = "'DZ', 'RL', 'IB', 'LV', 'EX','CC','GR', 'LO'"
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
    AND HUB = 'Iron Bridge'
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
    WHERE ASSET_PREFIX ='RD'
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
            WHEN h.IS_MOVING = 1 THEN 'HT_RD_MOVING'
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
    if not INTERSECTION_POLYGONS:
        return empty_intersection_gdf(TARGET_CRS)

    records = []
    for name, polygon in INTERSECTION_POLYGONS:
        if polygon is None or polygon.is_empty:
            continue
        records.append({
            "INTERSECTION_NAME": name,
            "DETECTION_METHOD": "MANUAL_BASE",
            "MANUAL_EDIT": "BASE",
            "geometry": polygon,
        })

    if not records:
        return empty_intersection_gdf(TARGET_CRS)

    manual_gdf = gpd.GeoDataFrame(
        records, geometry="geometry", crs=SOURCE_CRS
    )
    manual_gdf["INTERSECTION_X"] = manual_gdf.geometry.centroid.x
    manual_gdf["INTERSECTION_Y"] = manual_gdf.geometry.centroid.y
    return manual_gdf.to_crs(TARGET_CRS)


def classify_area_categories(gdf, lane_gdf, intersection_gdf):
    """Classify points using GeoPandas spatial indexes instead of Python loops."""
    if gdf.empty:
        return gdf

    gdf = ensure_geodataframe(gdf, SOURCE_CRS, "Interaction points")
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

    classified = gpd.GeoDataFrame(
        classified, geometry="geometry", crs=TARGET_CRS
    )
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

    if "geometry" not in plot_df.columns:
        raise ValueError(
            "Processed interaction data has no geometry column. "
            f"Columns: {list(plot_df.columns)}"
        )

    plot_df = plot_df[plot_df["geometry"].notna()].copy()

    if plot_df.empty:
        return empty_geodataframe(TARGET_CRS)

    gdf_source = gpd.GeoDataFrame(
        plot_df, geometry="geometry", crs=SOURCE_CRS
    )
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
intersection_gdf = concat_geodataframes(
    [manual_intersections_gdf, dxf_intersections_gdf],
    crs=TARGET_CRS,
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
# DASH APP - PERFORMANCE-OPTIMISED LOADING
# -----------------------------
# The date inputs no longer trigger Snowflake on every keystroke.
# Data is loaded once when the user clicks "Load Data" and remains in the
# server-side LRU cache. The browser stores only the date-range cache key.
MAX_DASH_POINT_MARKERS = 5000

app = Dash(__name__)

app.layout = html.Div(
    style={"height": "100vh"},
    children=[
        html.Div(
            "Eliwana Haul Road - HT/RD/HOV Interactions Within 50 m",
            style={
                "padding": "10px",
                "fontSize": "20px",
                "fontWeight": "bold",
            },
        ),

        # Stores only the active server-cache key, not the GeoDataFrame.
        dcc.Store(id="loaded-data-key", storage_type="memory"),

        html.Div(
            style={
                "padding": "10px",
                "display": "flex",
                "gap": "12px",
                "alignItems": "end",
                "flexWrap": "wrap",
            },
            children=[
                html.Div([
                    html.Label("Start Datetime"),
                    dcc.Input(
                        id="start-datetime",
                        type="text",
                        value=DEFAULT_START_DATETIME,
                        placeholder="YYYY-MM-DD HH:MM:SS",
                        debounce=True,
                        style={"width": "180px"},
                    ),
                ]),
                html.Div([
                    html.Label("End Datetime"),
                    dcc.Input(
                        id="end-datetime",
                        type="text",
                        value=DEFAULT_END_DATETIME,
                        placeholder="YYYY-MM-DD HH:MM:SS",
                        debounce=True,
                        style={"width": "180px"},
                    ),
                ]),
                html.Button(
                    "Load Data",
                    id="load-data-button",
                    n_clicks=0,
                    style={
                        "height": "36px",
                        "padding": "0 18px",
                        "fontWeight": "bold",
                        "cursor": "pointer",
                    },
                ),
                dcc.Loading(
                    type="circle",
                    children=html.Div(
                        id="load-status",
                        children="Select a date range and click Load Data.",
                        style={"minWidth": "260px"},
                    ),
                ),
            ],
        ),

        html.Div(
            style={"padding": "10px"},
            children=[
                html.Label("Select Asset types"),
                dcc.Dropdown(
                    id="machine-dropdown",
                    options=[],
                    value=[],
                    multi=True,
                    disabled=True,
                ),
            ],
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
                    allowCross=False,
                    disabled=True,
                    updatemode="mouseup",
                ),
            ],
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
                html.Span("Asset Type:"),

                *[
                    html.Span([
                        html.Span(
                            style={
                                "display": "inline-block",
                                "width": "12px",
                                "height": "12px",
                                "backgroundColor": colour,
                                "border": "1px solid black",
                                "marginRight": "5px",
                                "verticalAlign": "middle",
                            }
                        ),
                        prefix,
                    ])
                    for prefix, colour in PREFIX_COLORS.items()
                ],

                html.Span([
                    html.Span(
                        style={
                            "display": "inline-block",
                            "width": "12px",
                            "height": "12px",
                            "backgroundColor": DEFAULT_PREFIX_COLOR,
                            "border": "1px solid black",
                            "marginRight": "5px",
                            "verticalAlign": "middle",
                        }
                    ),
                    "Other",
                ]),
            ],
        ),

        dcc.Loading(
            type="circle",
            children=dl.Map(
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
            ),
        ),
    ],
)


# -----------------------------
# LOAD DATA ONCE
# -----------------------------
@app.callback(
    Output("loaded-data-key", "data"),
    Output("machine-dropdown", "options"),
    Output("machine-dropdown", "value"),
    Output("machine-dropdown", "disabled"),
    Output("time-slider", "min"),
    Output("time-slider", "max"),
    Output("time-slider", "value"),
    Output("time-slider", "marks"),
    Output("time-slider", "disabled"),
    Output("load-status", "children"),
    Input("load-data-button", "n_clicks"),
    State("start-datetime", "value"),
    State("end-datetime", "value"),
    prevent_initial_call=True,
)
def load_dashboard_data(n_clicks, start_date, end_date):
    if not n_clicks:
        return (no_update,) * 10

    start_date = clean_datetime(start_date, DEFAULT_START_DATETIME)
    end_date = clean_datetime(end_date, DEFAULT_END_DATETIME)

    try:
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
    except Exception:
        return (
            None, [], [], True, 0, 1, [0, 1], {0: "Invalid date"}, True,
            "Invalid datetime format. Use YYYY-MM-DD HH:MM:SS.",
        )

    if end_ts <= start_ts:
        return (
            None, [], [], True, 0, 1, [0, 1], {0: "Invalid range"}, True,
            "End datetime must be later than start datetime.",
        )

    # Normalise the key so equivalent input strings share the same cache entry.
    cache_start = start_ts.strftime("%Y-%m-%d %H:%M:%S")
    cache_end = end_ts.strftime("%Y-%m-%d %H:%M:%S")

    try:
        gdf_plot = get_processed_pair_data(cache_start, cache_end)
    except Exception as exc:
        debug_log("Dash data load failed", traceback.format_exc())
        return (
            None, [], [], True, 0, 1, [0, 1], {0: "Load failed"}, True,
            f"Data load failed: {exc}",
        )

    if gdf_plot.empty:
        return (
            {"start": cache_start, "end": cache_end},
            [], [], True, 0, 1, [0, 1], {0: "No data"}, True,
            "No interactions were returned for this date range.",
        )

    machines = sorted(
        gdf_plot.loc[gdf_plot["TYPE"] == "HOV", "PREFIX"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )
    machine_options = [{"label": m, "value": m} for m in machines]

    min_time = int(gdf_plot["TIME_INT"].min())
    max_time = int(gdf_plot["TIME_INT"].max())
    minimum_timestamp = gdf_plot["TIMESTAMP"].min()

    mark_values = (
        pd.Series(
            np.linspace(
                min_time,
                max_time,
                num=min(9, max(max_time - min_time + 1, 2)),
            )
        )
        .round()
        .astype(int)
        .drop_duplicates()
        .tolist()
    )

    time_marks = {
        int(value): (minimum_timestamp + pd.Timedelta(seconds=int(value))).strftime("%H:%M")
        for value in mark_values
    }
    time_marks[max_time] = (
        minimum_timestamp + pd.Timedelta(seconds=max_time)
    ).strftime("%H:%M")

    return (
        {"start": cache_start, "end": cache_end},
        machine_options,
        machines,
        False,
        min_time,
        max_time,
        [min_time, max_time],
        time_marks,
        False,
        f"Loaded {len(gdf_plot):,} map records for {cache_start} to {cache_end}.",
    )


# -----------------------------
# UPDATE MAP FROM SERVER CACHE ONLY
# -----------------------------
@app.callback(
    Output("dynamic-layers", "children"),
    Input("machine-dropdown", "value"),
    Input("time-slider", "value"),
    Input("loaded-data-key", "data"),
    prevent_initial_call=True,
)
def update_map(selected_machines, time_range, loaded_data_key):
    if not loaded_data_key or not selected_machines or not time_range:
        return []

    start_date = loaded_data_key.get("start")
    end_date = loaded_data_key.get("end")
    if not start_date or not end_date:
        return []

    # This is an in-memory cache lookup after the Load Data callback.
    gdf_plot = get_processed_pair_data(start_date, end_date)
    if gdf_plot.empty:
        return []

    start_time, end_time = map(int, time_range)
    selected_set = set(selected_machines)

    mask = (
        (
            (gdf_plot["TYPE"].eq("HT") & gdf_plot["PAIR_PREFIX"].isin(selected_set))
            | (gdf_plot["TYPE"].eq("HOV") & gdf_plot["PREFIX"].isin(selected_set))
        )
        & gdf_plot["TIME_INT"].between(start_time, end_time, inclusive="both")
    )

    filtered = gdf_plot.loc[mask].dropna(subset=["lon", "lat"]).copy()
    if filtered.empty:
        return []

    # Leaflet becomes slow when thousands of individual Dash components are sent.
    # Keep the temporal and asset coverage while limiting browser component count.
    if len(filtered) > MAX_DASH_POINT_MARKERS:
        filtered = (
            filtered.sort_values("TIME_INT")
            .iloc[::math.ceil(len(filtered) / MAX_DASH_POINT_MARKERS)]
            .copy()
        )

    layers = []
    for row in filtered.itertuples(index=False):
        area_category = str(
            getattr(row, "AREA_CATEGORY", "")
        ).strip().lower()

        prefix = str(
            getattr(row, "PREFIX", "")
        ).strip().upper()

        marker_color = PREFIX_COLORS.get(
            prefix,
            DEFAULT_PREFIX_COLOR,
        )
        intersection_name = getattr(row, "INTERSECTION_NAME", None) or "-"
        distance_to_lane = getattr(row, "DISTANCE_TO_LANE_M", float("nan"))

        layers.append(
            dl.CircleMarker(
                center=[row.lat, row.lon],
                radius=4,
                color=marker_color,
                fillColor=marker_color,
                fill=True,
                fillOpacity=0.85,
                weight=2,
                children=[
                    dl.Tooltip([
                        html.Div(f"Type: {row.TYPE}"),
                        html.Div(f"Machine: {row.MACHINE}"),
                        html.Div(f"Asset Type: {row.PREFIX}"),
                        html.Div(f"Near Machine: {row.PAIR_MACHINE}"),
                        html.Div(f"Near Asset Type: {row.PAIR_PREFIX}"),
                        html.Div(f"Time: {row.TIMESTAMP_STR}"),
                        html.Div(f"Distance: {row.DISTANCE_METRES:.2f} m"),
                        html.Div(f"Interaction: {row.INTERACTION_TYPE}"),
                        html.Div(f"Moving: {row.MOVING_STATUS}"),
                        html.Div(f"Operational Area: {area_category.title()}"),
                        html.Div(f"Intersection: {intersection_name}"),
                        html.Div(
                            "Distance to lane: -"
                            if pd.isna(distance_to_lane)
                            else f"Distance to lane: {distance_to_lane:.2f} m"
                        ),
                        html.Div(f"X: {row.X:.2f}"),
                        html.Div(f"Y: {row.Y:.2f}"),
                    ])
                ],
            )
        )

    return layers


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(
        debug=True,
        port=8002,
        use_reloader=False,
    )