import pandas as pd
import geopandas as gpd
from shapely import wkt
import snowflake.connector
from sqlalchemy import create_engine
import ezdxf
import math
import traceback
from datetime import datetime
import networkx as nx
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union, nearest_points
from shapely.affinity import translate
from lxml import etree
import os
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
DXF_PATH = r"Ironbridge/Layers/Ironbridge_dxf.dxf"
KML_FILE = r"Ironbridge/Layers/Ironbridge_Layer.kml"
GPKG_PATH = r"Ironbridge/Layers/Ironbridge_package.gpkg"
WMS_URL = "https://fortescuesky.fmgl.com.au/SG/default/streamer.ashx?"

SOURCE_CRS = "EPSG:28350"
TARGET_CRS = "EPSG:4326"

# DEFAULT_START_DATETIME = "2026-05-09 11:19:01"
# DEFAULT_END_DATETIME = "2026-05-09 13:19:01"

DEFAULT_START_DATETIME = "2026-07-16 11:00:00"
DEFAULT_END_DATETIME = "2026-07-16 13:00:00"


HOV_ASSET_TYPES = ["RD", "RL", "IB", "LV", "EX", "CC", "GR", "LO"]

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
    # {
    #     "SOURCE": "Centerline Intersection 241",
    #     "NEW_NAME": "Centerline Intersection 500",
    #     "OFFSET_XY": (380, -80),
    # },
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
# GEOMETRY-SAFE HELPERS
# -----------------------------
def ensure_geodataframe(value, crs, name="GeoDataFrame"):
    """
    Return a GeoDataFrame with an active geometry column.

    This avoids:
        ValueError: Unknown column geometry
    after pandas concat/filter operations.
    """
    if value is None:
        return gpd.GeoDataFrame(
            geometry=gpd.GeoSeries([], crs=crs),
            crs=crs,
        )

    if isinstance(value, gpd.GeoDataFrame):
        frame = value.copy()
    else:
        frame = pd.DataFrame(value).copy()

    if "geometry" not in frame.columns:
        if len(frame) == 0:
            return gpd.GeoDataFrame(
                frame,
                geometry=gpd.GeoSeries([], crs=crs),
                crs=crs,
            )

        raise ValueError(
            f"{name} has no geometry column. "
            f"Columns: {list(frame.columns)}"
        )

    return gpd.GeoDataFrame(
        frame,
        geometry="geometry",
        crs=getattr(value, "crs", None) or crs,
    )


def concat_geodataframes(frames, crs):
    """
    Concatenate GeoDataFrames without losing the active geometry column.
    """
    valid_frames = []

    for frame in frames:
        if frame is None:
            continue

        frame = ensure_geodataframe(
            frame,
            crs=crs,
            name="Concatenated frame",
        )

        if frame.empty:
            continue

        if frame.crs != crs:
            frame = frame.to_crs(crs)

        valid_frames.append(frame)

    if not valid_frames:
        return empty_intersection_gdf(crs=crs)

    combined_table = pd.concat(
        [pd.DataFrame(frame.copy()) for frame in valid_frames],
        ignore_index=True,
        sort=False,
    )

    return gpd.GeoDataFrame(
        combined_table,
        geometry="geometry",
        crs=crs,
    )


def validate_manual_polygon(name, polygon):
    if polygon is None:
        raise ValueError(f"Manual polygon {name!r} is None.")

    if polygon.is_empty:
        raise ValueError(f"Manual polygon {name!r} is empty.")

    if polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(
            f"Manual polygon {name!r} must be Polygon or MultiPolygon, "
            f"not {polygon.geom_type}."
        )

    if not polygon.is_valid:
        polygon = polygon.buffer(0)

    if polygon.is_empty:
        raise ValueError(
            f"Manual polygon {name!r} could not be repaired."
        )

    return polygon

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
def _extract_dxf_entity_coordinates(entity):
    entity_type = entity.dxftype()

    if entity_type == "LINE":
        return [
            (float(entity.dxf.start.x), float(entity.dxf.start.y)),
            (float(entity.dxf.end.x), float(entity.dxf.end.y)),
        ], False

    if entity_type == "LWPOLYLINE":
        points = [
            (float(point[0]), float(point[1]))
            for point in entity.get_points("xy")
        ]
        return points, bool(entity.closed)

    if entity_type == "POLYLINE":
        points = [
            (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
            for vertex in entity.vertices
        ]
        return points, bool(entity.is_closed)

    return None, False


def load_dxf_lanes(dxf_path):
    """Load lane-like LINE/LWPOLYLINE/POLYLINE geometry safely."""
    if not os.path.exists(dxf_path):
        raise FileNotFoundError(f"DXF file not found: {dxf_path}")

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
        points, is_closed = _extract_dxf_entity_coordinates(entity)

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
        available = ", ".join(sorted(available_layers))
        raise ValueError(
            "No usable LINE, LWPOLYLINE or POLYLINE geometry was found. "
            f"Available DXF layers: {available}"
        )

    if not lane_records:
        debug_log(
            "No DXF layer containing 'LANE' was found. "
            "Using all supported line entities."
        )

    dxf_gdf = gpd.GeoDataFrame(
        records,
        geometry="geometry",
        crs=SOURCE_CRS,
    )
    dxf_gdf = dxf_gdf[
        dxf_gdf.geometry.notna() & ~dxf_gdf.geometry.is_empty
    ].copy()

    if dxf_gdf.empty:
        raise ValueError("DXF entities were found, but no valid geometry remained.")

    dxf_gdf = dxf_gdf.to_crs(TARGET_CRS)
    dxf_gdf["geometry"] = dxf_gdf.geometry.simplify(
        0.000003,
        preserve_topology=True,
    )

    return gpd.GeoDataFrame(
        dxf_gdf,
        geometry="geometry",
        crs=TARGET_CRS,
    )

# -----------------------------
# DETECT INTERSECTIONS (HYBRID)
# -----------------------------
def detect_intersections_hybrid(lane_gdf):
    """Use the centreline graph detector without recursive self-calls."""
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


def export_intersections_csv(intersection_gdf, output_path="ironbridge_detected_intersections.csv"):
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
    Apply manual polygon edits in this order:

      1. DELETE
      2. REPLACE
      3. MOVE
      4. SWAP
      5. ADD
      6. COPY

    All manual polygon coordinates and offsets use SOURCE_CRS metres.
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

    edited = ensure_geodataframe(
        intersection_gdf,
        crs=TARGET_CRS,
        name="intersection_gdf",
    )

    if edited.empty:
        edited = empty_intersection_gdf(crs=SOURCE_CRS)
    else:
        edited = edited.to_crs(SOURCE_CRS)

    for column in expected_columns:
        if column not in edited.columns:
            edited[column] = None

    # --------------------------------------------------
    # DELETE
    # --------------------------------------------------
    if not edited.empty and MANUAL_DELETE_INTERSECTIONS:
        edited = edited[
            ~edited["INTERSECTION_NAME"]
            .astype(str)
            .isin(MANUAL_DELETE_INTERSECTIONS)
        ].copy()

        edited = ensure_geodataframe(
            edited,
            crs=SOURCE_CRS,
            name="Edited intersections after delete",
        )

    # --------------------------------------------------
    # REPLACE polygon geometry
    # --------------------------------------------------
    for intersection_name, replacement_polygon in (
        MANUAL_REPLACE_INTERSECTION_POLYGONS.items()
    ):
        replacement_polygon = validate_manual_polygon(
            intersection_name,
            replacement_polygon,
        )

        mask = (
            edited["INTERSECTION_NAME"].astype(str)
            == str(intersection_name)
        )

        if not mask.any():
            debug_log(
                f"Manual replace target not found: {intersection_name}"
            )
            continue

        indices = edited.index[mask]

        for idx in indices:
            edited.at[idx, "geometry"] = replacement_polygon

        edited.loc[mask, "MANUAL_EDIT"] = "REPLACE"
        edited.loc[mask, "DETECTION_METHOD"] = (
            edited.loc[mask, "DETECTION_METHOD"]
            .fillna("AUTO")
            .astype(str)
            + "+MANUAL_REPLACE"
        )

    # --------------------------------------------------
    # MOVE polygon geometry
    # --------------------------------------------------
    for intersection_name, offsets in MANUAL_MOVE_INTERSECTIONS.items():
        x_offset_m, y_offset_m = offsets

        mask = (
            edited["INTERSECTION_NAME"].astype(str)
            == str(intersection_name)
        )

        if not mask.any():
            debug_log(
                f"Manual move target not found: {intersection_name}"
            )
            continue

        for idx in edited.index[mask]:
            geometry = edited.at[idx, "geometry"]

            if geometry is None or geometry.is_empty:
                continue

            edited.at[idx, "geometry"] = translate(
                geometry,
                xoff=x_offset_m,
                yoff=y_offset_m,
            )

        edited.loc[mask, "MANUAL_EDIT"] = "MOVE"
        edited.loc[mask, "DETECTION_METHOD"] = (
            edited.loc[mask, "DETECTION_METHOD"]
            .fillna("AUTO")
            .astype(str)
            + "+MANUAL_MOVE"
        )

    # --------------------------------------------------
    # SWAP polygon geometry
    # --------------------------------------------------
    for name_a, name_b in MANUAL_SWAP_INTERSECTIONS:
        mask_a = (
            edited["INTERSECTION_NAME"].astype(str)
            == str(name_a)
        )
        mask_b = (
            edited["INTERSECTION_NAME"].astype(str)
            == str(name_b)
        )

        if not mask_a.any() or not mask_b.any():
            debug_log(
                f"Manual swap target missing: {name_a}, {name_b}"
            )
            continue

        index_a = edited.index[mask_a][0]
        index_b = edited.index[mask_b][0]

        geometry_a = edited.at[index_a, "geometry"]
        geometry_b = edited.at[index_b, "geometry"]

        edited.at[index_a, "geometry"] = geometry_b
        edited.at[index_b, "geometry"] = geometry_a

        edited.at[index_a, "MANUAL_EDIT"] = "SWAP"
        edited.at[index_b, "MANUAL_EDIT"] = "SWAP"

        edited.at[index_a, "DETECTION_METHOD"] = (
            str(edited.at[index_a, "DETECTION_METHOD"] or "AUTO")
            + "+MANUAL_SWAP"
        )
        edited.at[index_b, "DETECTION_METHOD"] = (
            str(edited.at[index_b, "DETECTION_METHOD"] or "AUTO")
            + "+MANUAL_SWAP"
        )

    # --------------------------------------------------
    # ADD completely new polygons
    # --------------------------------------------------
    add_records = []

    for intersection_name, polygon in MANUAL_ADD_INTERSECTION_POLYGONS:
        polygon = validate_manual_polygon(
            intersection_name,
            polygon,
        )

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
        add_gdf = gpd.GeoDataFrame(
            add_records,
            geometry="geometry",
            crs=SOURCE_CRS,
        )

        edited = concat_geodataframes(
            [edited, add_gdf],
            crs=SOURCE_CRS,
        )

    # --------------------------------------------------
    # COPY existing polygons
    # --------------------------------------------------
    copied_records = []

    for rule in MANUAL_COPY_INTERSECTIONS:
        source_name = rule["SOURCE"]
        new_name = rule["NEW_NAME"]
        x_offset, y_offset = rule.get("OFFSET_XY", (0, 0))

        source = edited[
            edited["INTERSECTION_NAME"].astype(str)
            == str(source_name)
        ]

        if source.empty:
            debug_log(
                f"Manual copy source not found: {source_name}"
            )
            continue

        source_row = source.iloc[0]
        source_geometry = source_row.geometry

        if source_geometry is None or source_geometry.is_empty:
            continue

        new_geometry = translate(
            source_geometry,
            xoff=x_offset,
            yoff=y_offset,
        )

        new_record = source_row.to_dict()
        new_record["INTERSECTION_NAME"] = new_name
        new_record["geometry"] = new_geometry
        new_record["INTERSECTION_X"] = new_geometry.centroid.x
        new_record["INTERSECTION_Y"] = new_geometry.centroid.y
        new_record["MANUAL_EDIT"] = "COPY"
        new_record["DETECTION_METHOD"] = "MANUAL_COPY"

        copied_records.append(new_record)

    if copied_records:
        copied_gdf = gpd.GeoDataFrame(
            copied_records,
            geometry="geometry",
            crs=SOURCE_CRS,
        )

        edited = concat_geodataframes(
            [edited, copied_gdf],
            crs=SOURCE_CRS,
        )

    # --------------------------------------------------
    # PRINT selected polygons for copying into config
    # --------------------------------------------------
    for intersection_name in PRINT_INTERSECTIONS_POLYGONS:
        selected = edited[
            edited["INTERSECTION_NAME"].astype(str)
            == str(intersection_name)
        ]

        if selected.empty:
            continue

        for _, row in selected.iterrows():
            geometry = row.geometry

            print(
                f"\n{intersection_name} WKT:\n"
                f"{geometry.wkt}\n"
            )

            if geometry.geom_type == "Polygon":
                coordinates = list(geometry.exterior.coords)

                print(
                    f'{intersection_name} Python polygon:\n'
                    "Polygon([\n"
                    + "".join(
                        f"    ({x:.6f}, {y:.6f}),\n"
                        for x, y in coordinates
                    )
                    + "])\n"
                )

    if edited.empty:
        return empty_intersection_gdf(crs=TARGET_CRS)

    edited = ensure_geodataframe(
        edited,
        crs=SOURCE_CRS,
        name="Final manually edited intersections",
    )

    edited = edited[
        edited.geometry.notna()
        & ~edited.geometry.is_empty
    ].copy()

    edited = ensure_geodataframe(
        edited,
        crs=SOURCE_CRS,
        name="Filtered manually edited intersections",
    )

    edited["INTERSECTION_X"] = edited.geometry.centroid.x
    edited["INTERSECTION_Y"] = edited.geometry.centroid.y

    debug_columns = [
        "INTERSECTION_NAME",
        "DETECTION_METHOD",
        "MANUAL_EDIT",
        "INTERSECTION_X",
        "INTERSECTION_Y",
    ]

    debug_log(
        "Manual intersection edits applied",
        edited[debug_columns],
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
    """Parse KML coordinates into GeoJSON [longitude, latitude] order."""
    coordinates = []

    if not text:
        return coordinates

    for point in text.strip().split():
        values = point.split(",")
        if len(values) < 2:
            continue
        coordinates.append([float(values[0]), float(values[1])])

    return coordinates


def kml_to_geojson(kml_file):
    """
    Read standard KML Placemark geometry only.
    Skyline sx:RasterLayer elements are ignored because WMS is loaded separately.
    """
    if not os.path.exists(kml_file):
        debug_log(f"KML file not found: {kml_file}. Continuing without it.")
        return {"type": "FeatureCollection", "features": []}

    namespaces = {"kml": "http://www.opengis.net/kml/2.2"}
    tree = etree.parse(str(kml_file))
    features = []

    for placemark in tree.xpath(".//kml:Placemark", namespaces=namespaces):
        name = placemark.findtext("kml:name", namespaces=namespaces)
        properties = {"name": name or "Unnamed feature"}

        polygon_nodes = placemark.xpath(
            ".//kml:Polygon/kml:outerBoundaryIs/kml:LinearRing/kml:coordinates",
            namespaces=namespaces,
        )
        line_nodes = placemark.xpath(
            ".//kml:LineString/kml:coordinates",
            namespaces=namespaces,
        )

        for node in polygon_nodes:
            coordinates = parse_coordinates(node.text)
            if len(coordinates) >= 3:
                features.append({
                    "type": "Feature",
                    "properties": properties.copy(),
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [coordinates],
                    },
                })

        for node in line_nodes:
            coordinates = parse_coordinates(node.text)
            if len(coordinates) >= 2:
                features.append({
                    "type": "Feature",
                    "properties": properties.copy(),
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coordinates,
                    },
                })

    return {"type": "FeatureCollection", "features": features}


# -----------------------------
# SNOWFLAKE DATA LOADER
# -----------------------------
def load_nearby_machine_data(start_date, end_date):
    """Load moving DZ-to-HOV interactions from Iron Bridge."""
    engine = create_engine(
        "snowflake://{user}:{password}@{account}/{database}/{schema}"
        "?warehouse={warehouse}"
        "&role={role}"
        "&authenticator=externalbrowser".format(
            user="FATEMEH.HAERI@FORTESCUE.COM",
            password="",
            account="FMG-WN74261",
            database="AA_OPERATIONS_MANAGEMENT",
            schema="SELFSERVICE",
            warehouse="WH_AA_OPERATIONS_MANAGEMENT",
            role="EDW_FATEMEH.HAERI",
        )
    )

    hov_type_sql = ", ".join(f"'{asset_type}'" for asset_type in HOV_ASSET_TYPES)

    query = f"""
WITH base AS (
    SELECT
        *,
        DATE_TRUNC('SECOND', "TIMESTAMP") AS TS_SECOND,
        LAG(X) OVER (PARTITION BY MACHINE ORDER BY "TIMESTAMP") AS PREV_X,
        LAG(Y) OVER (PARTITION BY MACHINE ORDER BY "TIMESTAMP") AS PREV_Y,
        LAG(Z) OVER (PARTITION BY MACHINE ORDER BY "TIMESTAMP") AS PREV_Z
    FROM AA_OPERATIONS_MANAGEMENT.SELFSERVICE.FMS_MACHINE_LOCATION
    WHERE "TIMESTAMP" BETWEEN '{start_date}' AND '{end_date}'
      AND HUB = 'Iron Bridge'
),
movement AS (
    SELECT
        *,
        LEFT(MACHINE, 2) AS ASSET_PREFIX,
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
    SELECT * FROM movement WHERE ASSET_PREFIX = 'DZ'
),
hov AS (
    SELECT * FROM movement WHERE ASSET_PREFIX IN ({hov_type_sql})
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
            WHEN ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) < 15 THEN 'NEAR_MISS'
            WHEN ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) <= 50 THEN 'PROCEDURE_BREACH'
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
            WHEN h.IS_MOVING = 1 THEN 'DZ_MOVING'
            WHEN l.IS_MOVING = 1 THEN 'HOV_MOVING'
        END AS MOVING_STATUS
    FROM ht h
    JOIN hov l
      ON h.TS_SECOND = l.TS_SECOND
     AND ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) <= 50
    WHERE h.IS_MOVING = 1
)
SELECT *
FROM matches
ORDER BY "TIMESTAMP", DISTANCE_METRES
"""

    try:
        with engine.connect() as connection:
            df = pd.read_sql(query, connection).rename(columns=str.upper)
    finally:
        engine.dispose()

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
    if gdf.empty:
        return gdf

    classified = gpd.GeoDataFrame(
        gdf.copy(),
        geometry="geometry",
        crs=gdf.crs,
    )
    points_source = classified.to_crs(SOURCE_CRS)
    lanes_source = gpd.GeoDataFrame(
        lane_gdf.copy(),
        geometry="geometry",
        crs=lane_gdf.crs,
    ).to_crs(SOURCE_CRS)

    def nearest_lane_attributes(point):
        if point is None or point.is_empty or lanes_source.empty:
            return {
                "DISTANCE_TO_LANE_M": None,
                "polyline_id": None,
                "lane_layer": None,
                "lane_vertex_count": None,
            }

        distances = lanes_source.geometry.distance(point)
        nearest_idx = distances.idxmin()
        nearest_lane = lanes_source.loc[nearest_idx]
        return {
            "DISTANCE_TO_LANE_M": float(distances.loc[nearest_idx]),
            "polyline_id": nearest_lane.get("polyline_id"),
            "lane_layer": nearest_lane.get("layer"),
            "lane_vertex_count": nearest_lane.get("vertex_count"),
        }

    lane_attributes = pd.DataFrame(
        [nearest_lane_attributes(point) for point in points_source.geometry],
        index=classified.index,
    )

    for column_name in lane_attributes.columns:
        classified[column_name] = lane_attributes[column_name]

    classified = gpd.GeoDataFrame(
        classified,
        geometry="geometry",
        crs=gdf.crs,
    )
    classified["ON_LANE"] = (
        pd.to_numeric(classified["DISTANCE_TO_LANE_M"], errors="coerce")
        <= LANE_BUFFER_METRES
    )
    classified["AREA_CATEGORY"] = "dynamic area"
    classified.loc[classified["ON_LANE"], "AREA_CATEGORY"] = "haul road"
    classified["INTERSECTION_NAME"] = None

    if intersection_gdf is not None and not intersection_gdf.empty:
        intersections_source = gpd.GeoDataFrame(
            intersection_gdf.copy(),
            geometry="geometry",
            crs=intersection_gdf.crs,
        ).to_crs(SOURCE_CRS)

        for idx, point in points_source.geometry.items():
            if point is None or point.is_empty:
                continue

            hits = intersections_source[
                intersections_source.geometry.contains(point)
                | (
                    intersections_source.geometry.distance(point)
                    <= INTERSECTION_POINT_TOLERANCE_METRES
                )
            ]

            if not hits.empty:
                classified.at[idx, "AREA_CATEGORY"] = "intersection"
                classified.at[idx, "INTERSECTION_NAME"] = hits.iloc[0]["INTERSECTION_NAME"]

    debug_log("classify_area_categories", classified)
    return gpd.GeoDataFrame(
        classified,
        geometry="geometry",
        crs=gdf.crs,
    )


def process_pair_data(df):
    if df.empty:
        return gpd.GeoDataFrame(geometry=[], crs=TARGET_CRS)

    df["TIMESTAMP"] = pd.to_datetime(
        df["TIMESTAMP"],
        format="%Y-%m-%d %H:%M:%S"
    )
    df["HT_GEOMETRY"] = df["HT_WKT_GEOM"].apply(wkt.loads)
    df["HOV_GEOMETRY"] = df["HOV_WKT_GEOM"].apply(wkt.loads)

    ht_df = df[[
        "HT_MACHINE",
        "HT_PREFIX",
        "HOV_MACHINE",
        "HOV_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "INTERACTION_TYPE",
        "MOVING_STATUS",
        "HT_IS_MOVING",
        "HOV_IS_MOVING",
        "HT_X",
        "HT_Y",
        "HT_Z",
        "HT_GEOMETRY"
    ]].copy()

    ht_df.columns = [
        "MACHINE",
        "PREFIX",
        "PAIR_MACHINE",
        "PAIR_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "INTERACTION_TYPE",
        "MOVING_STATUS",
        "IS_MOVING",
        "PAIR_IS_MOVING",
        "X",
        "Y",
        "Z",
        "geometry"
    ]

    ht_df["TYPE"] = "HT"

    hov_df = df[[
        "HOV_MACHINE",
        "HOV_PREFIX",
        "HT_MACHINE",
        "HT_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "INTERACTION_TYPE",
        "MOVING_STATUS",
        "HOV_IS_MOVING",
        "HT_IS_MOVING",
        "HOV_X",
        "HOV_Y",
        "HOV_Z",
        "HOV_GEOMETRY"
    ]].copy()

    hov_df.columns = [
        "MACHINE",
        "PREFIX",
        "PAIR_MACHINE",
        "PAIR_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "INTERACTION_TYPE",
        "MOVING_STATUS",
        "IS_MOVING",
        "PAIR_IS_MOVING",
        "X",
        "Y",
        "Z",
        "geometry"
    ]

    hov_df["TYPE"] = "HOV"

    plot_df = pd.concat([ht_df, hov_df], ignore_index=True)
    plot_df = plot_df.drop_duplicates(
        subset=["TYPE", "PREFIX", "PAIR_PREFIX", "MACHINE", "PAIR_MACHINE", "TIMESTAMP", "X", "Y"]
    )

    if "geometry" not in plot_df.columns:
        raise ValueError(
            "Processed interaction records have no geometry column."
        )

    plot_df = plot_df[
        plot_df["geometry"].notna()
    ].copy()

    if plot_df.empty:
        return gpd.GeoDataFrame(
            geometry=gpd.GeoSeries([], crs=TARGET_CRS),
            crs=TARGET_CRS,
        )

    gdf = gpd.GeoDataFrame(
        plot_df,
        geometry="geometry",
        crs=SOURCE_CRS
    ).to_crs(TARGET_CRS)

    gdf.sort_values(["TYPE", "PAIR_PREFIX", "TIMESTAMP"], inplace=True)

    gdf["lon"] = gdf.geometry.x
    gdf["lat"] = gdf.geometry.y
    gdf["TIMESTAMP_STR"] = gdf["TIMESTAMP"].astype(str).str.split("+").str[0]

    gdf["TIME_INT"] = (
        gdf["TIMESTAMP"] - gdf["TIMESTAMP"].min()
    ).dt.total_seconds().astype(int)
    debug_log("process_pair_data", gdf)
    return classify_area_categories(gdf, dxf_gdf, intersection_gdf)


# -----------------------------
# LOAD STATIC MAP DATA
# -----------------------------
geojson_data = kml_to_geojson(KML_FILE)
dxf_gdf = load_dxf_lanes(DXF_PATH)
manual_intersections_gdf = load_intersection_boxes()
dxf_intersections_gdf = detect_intersections_hybrid(dxf_gdf)
# export_intersections_csv(dxf_intersections_gdf)

# Keep the manually confirmed polygons and add the automatically detected
# ST_DWITHIN-style lane-pair clusters. Do not include the old GPKG endpoint
# detector here because it can over-detect ordinary haul-road joins.
intersection_gdf = concat_geodataframes(
    [
        manual_intersections_gdf,
        dxf_intersections_gdf,
    ],
    crs=TARGET_CRS,
)

# Apply manual corrections after automatic detection and manual base polygons.
# This lets you remove false positives, move polygons, replace polygons,
# or add missed intersections from the CONFIG section above.
intersection_gdf = apply_manual_intersection_edits(intersection_gdf)
export_intersections_csv(intersection_gdf, "ironbridge_intersections.csv")

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

    centre = row.geometry.centroid
    intersection_name = row.get("INTERSECTION_NAME", "-")

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
            "Iron Bridge - DZ/HOV Interactions Within 50 m",
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
            center=[-22.35, 116.95],
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
                    zoomToBounds=False,
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


# ============================================================
# UPDATE CONTROLS
# ============================================================
# @app.callback(
#     Output("machine-dropdown", "options"),
#     Output("machine-dropdown", "value"),
#     Output("time-slider", "min"),
#     Output("time-slider", "max"),
#     Output("time-slider", "value"),
#     Output("time-slider", "marks"),

#     Input("start-datetime", "value"),
#     Input("end-datetime", "value"),
# )
# def update_controls(start_date, end_date):
#     start_date = clean_datetime(
#         start_date,
#         DEFAULT_START_DATETIME,
#     )

#     end_date = clean_datetime(
#         end_date,
#         DEFAULT_END_DATETIME,
#     )

#     interaction_df = load_nearby_machine_data(
#         start_date,
#         end_date,
#     )

#     gdf_plot = process_pair_data(interaction_df)

#     if gdf_plot.empty:
#         return [], [], 0, 1, [0, 1], {
#             0: "No data",
#             1: "",
#         }

#     hov_prefixes = sorted(
#         gdf_plot.loc[
#             gdf_plot["TYPE"] == "HOV",
#             "PREFIX",
#         ].dropna().unique()
#     )

#     options = [
#         {
#             "label": prefix,
#             "value": prefix,
#         }
#         for prefix in hov_prefixes
#     ]

#     minimum_time = int(
#         gdf_plot["TIME_INT"].min()
#     )

#     maximum_time = int(
#         gdf_plot["TIME_INT"].max()
#     )

#     mark_count = 8
#     mark_step = max(
#         (maximum_time - minimum_time) // mark_count,
#         1,
#     )

#     minimum_timestamp = (
#         gdf_plot["TIMESTAMP"].min()
#     )

#     marks = {}

#     for time_value in range(
#         minimum_time,
#         maximum_time + 1,
#         mark_step,
#     ):
#         label_time = (
#             minimum_timestamp
#             + pd.Timedelta(seconds=time_value)
#         )

#         marks[time_value] = (
#             label_time.strftime("%H:%M")
#         )

#     marks[maximum_time] = (
#         minimum_timestamp
#         + pd.Timedelta(seconds=maximum_time)
#     ).strftime("%H:%M")

#     return (
#         options,
#         hov_prefixes,
#         minimum_time,
#         maximum_time,
#         [minimum_time, maximum_time],
#         marks,
#     )


# # ============================================================
# # UPDATE MAP
# # ============================================================
# @app.callback(
#     Output("dynamic-layers", "children"),

#     Input("machine-dropdown", "value"),
#     Input("time-slider", "value"),
#     Input("start-datetime", "value"),
#     Input("end-datetime", "value"),
# )
# def update_map(
#     selected_hov_prefixes,
#     time_range,
#     start_date,
#     end_date,
# ):
#     if not selected_hov_prefixes:
#         return []

#     start_date = clean_datetime(
#         start_date,
#         DEFAULT_START_DATETIME,
#     )

#     end_date = clean_datetime(
#         end_date,
#         DEFAULT_END_DATETIME,
#     )

#     start_time, end_time = time_range

#     interaction_df = load_nearby_machine_data(
#         start_date,
#         end_date,
#     )

#     gdf_plot = process_pair_data(interaction_df)

#     if gdf_plot.empty:
#         return []

#     gdf_plot = gdf_plot[
#         (
#             (
#                 (gdf_plot["TYPE"] == "HT")
#                 & (
#                     gdf_plot["PAIR_PREFIX"].isin(
#                         selected_hov_prefixes
#                     )
#                 )
#             )
#             |
#             (
#                 (gdf_plot["TYPE"] == "HOV")
#                 & (
#                     gdf_plot["PREFIX"].isin(
#                         selected_hov_prefixes
#                     )
#                 )
#             )
#         )
#         & (
#             gdf_plot["TIME_INT"]
#             >= int(start_time)
#         )
#         & (
#             gdf_plot["TIME_INT"]
#             <= int(end_time)
#         )
#     ].copy()

#     if gdf_plot.empty:
#         return []

#     prefix_colors = {
#         "RD": "blue",
#         "RL": "green",
#         "IB": "orange",
#         "LV": "red",
#         "EX": "black",
#         "CC": "purple",
#         "GR": "brown",
#         "LO": "pink",
#     }

#     layers = []

#     # --------------------------------------------------------
#     # First DZ point in each continuous interaction sequence
#     # --------------------------------------------------------
#     dz_points = (
#         gdf_plot[
#             gdf_plot["TYPE"] == "HT"
#         ]
#         .dropna(subset=["lon", "lat"])
#         .sort_values(
#             [
#                 "MACHINE",
#                 "PAIR_MACHINE",
#                 "TIME_INT",
#             ]
#         )
#         .copy()
#     )

#     dz_points["PREV_TIME_INT"] = (
#         dz_points.groupby(
#             [
#                 "MACHINE",
#                 "PAIR_MACHINE",
#             ]
#         )["TIME_INT"].shift()
#     )

#     dz_points["NEW_INTERACTION"] = (
#         dz_points["PREV_TIME_INT"].isna()
#         |
#         (
#             (
#                 dz_points["TIME_INT"]
#                 - dz_points["PREV_TIME_INT"]
#             )
#             > MAX_INTERACTION_GAP_SECONDS
#         )
#     )

#     dz_points["INTERACTION_ID"] = (
#         dz_points.groupby(
#             [
#                 "MACHINE",
#                 "PAIR_MACHINE",
#             ]
#         )["NEW_INTERACTION"].cumsum()
#     )

#     first_dz_points = (
#         dz_points.sort_values(
#             [
#                 "MACHINE",
#                 "PAIR_MACHINE",
#                 "INTERACTION_ID",
#                 "TIME_INT",
#             ]
#         )
#         .drop_duplicates(
#             subset=[
#                 "MACHINE",
#                 "PAIR_MACHINE",
#                 "INTERACTION_ID",
#             ],
#             keep="first",
#         )
#     )

#     # --------------------------------------------------------
#     # Near-miss markers: closest point per machine pair
#     # --------------------------------------------------------
#     near_miss_points = (
#         gdf_plot[
#             (
#                 gdf_plot["TYPE"] == "HT"
#             )
#             & (
#                 gdf_plot["INTERACTION_TYPE"]
#                 == "NEAR_MISS"
#             )
#             & (
#                 gdf_plot["DISTANCE_METRES"] < 15
#             )
#         ]
#         .dropna(subset=["lon", "lat"])
#         .sort_values("DISTANCE_METRES")
#         .drop_duplicates(
#             subset=[
#                 "MACHINE",
#                 "PAIR_MACHINE",
#             ],
#             keep="first",
#         )
#     )

#     for row in near_miss_points.itertuples():
#         layers.append(
#             dl.DivMarker(
#                 position=[row.lat, row.lon],
#                 iconOptions={
#                     "html": (
#                         "<div style='"
#                         "font-size:18px;"
#                         "font-weight:bold;"
#                         "color:red;"
#                         "background:white;"
#                         "border:2px solid red;"
#                         "border-radius:50%;"
#                         "width:22px;"
#                         "height:22px;"
#                         "line-height:20px;"
#                         "text-align:center;'>"
#                         "!</div>"
#                     ),
#                     "className": "near-miss-marker",
#                     "iconSize": [22, 22],
#                     "iconAnchor": [11, 11],
#                 },
#                 children=[
#                     dl.Tooltip(
#                         [
#                             html.Div(
#                                 "Near miss: distance < 15 m"
#                             ),
#                             html.Div(
#                                 f"DZ: {row.MACHINE}"
#                             ),
#                             html.Div(
#                                 f"HOV: {row.PAIR_MACHINE}"
#                             ),
#                             html.Div(
#                                 f"Time: {row.TIMESTAMP_STR}"
#                             ),
#                             html.Div(
#                                 f"Distance: "
#                                 f"{row.DISTANCE_METRES:.2f} m"
#                             ),
#                             html.Div(
#                                 f"Area: {row.AREA_CATEGORY}"
#                             ),
#                         ]
#                     )
#                 ],
#             )
#         )

#     # --------------------------------------------------------
#     # Interaction point markers
#     # --------------------------------------------------------
#     point_gdf = gdf_plot.dropna(
#         subset=["lon", "lat"]
#     ).copy()

#     if len(point_gdf) > MAX_POINT_MARKERS:
#         sample_step = max(
#             1,
#             math.ceil(
#                 len(point_gdf)
#                 / MAX_POINT_MARKERS
#             ),
#         )

#         point_gdf = point_gdf.iloc[
#             ::sample_step
#         ].copy()

#     for row in point_gdf.itertuples():
#         marker_color = (
#             "cyan"
#             if row.TYPE == "HT"
#             else prefix_colors.get(
#                 row.PREFIX,
#                 "gray",
#             )
#         )

#         layers.append(
#             dl.CircleMarker(
#                 center=[row.lat, row.lon],
#                 radius=4,
#                 color=marker_color,
#                 fillColor=marker_color,
#                 fill=True,
#                 fillOpacity=0.85,
#                 weight=2,
#                 children=[
#                     dl.Tooltip(
#                         [
#                             html.Div(
#                                 f"Type: "
#                                 f"{'DZ' if row.TYPE == 'HT' else 'HOV'}"
#                             ),
#                             html.Div(
#                                 f"Machine: {row.MACHINE}"
#                             ),
#                             html.Div(
#                                 f"Asset type: {row.PREFIX}"
#                             ),
#                             html.Div(
#                                 f"Near machine: "
#                                 f"{row.PAIR_MACHINE}"
#                             ),
#                             html.Div(
#                                 f"Near asset type: "
#                                 f"{row.PAIR_PREFIX}"
#                             ),
#                             html.Div(
#                                 f"Time: {row.TIMESTAMP_STR}"
#                             ),
#                             html.Div(
#                                 f"Distance: "
#                                 f"{row.DISTANCE_METRES:.2f} m"
#                             ),
#                             html.Div(
#                                 f"Interaction: "
#                                 f"{row.INTERACTION_TYPE}"
#                             ),
#                             html.Div(
#                                 f"Moving: "
#                                 f"{row.MOVING_STATUS}"
#                             ),
#                             html.Div(
#                                 f"Area: "
#                                 f"{row.AREA_CATEGORY}"
#                             ),
#                             html.Div(
#                                 f"Distance to lane: "
#                                 f"{row.DISTANCE_TO_LANE_M:.2f} m"
#                             ),
#                             html.Div(
#                                 f"X: {row.X:.2f}"
#                             ),
#                             html.Div(
#                                 f"Y: {row.Y:.2f}"
#                             ),
#                         ]
#                     )
#                 ],
#             )
#         )

#     # --------------------------------------------------------
#     # First DZ point X markers
#     # --------------------------------------------------------
#     for row in first_dz_points.itertuples():
#         layers.append(
#             dl.DivMarker(
#                 position=[row.lat, row.lon],
#                 iconOptions={
#                     "html": (
#                         "<div style='"
#                         "font-size:22px;"
#                         "font-weight:bold;"
#                         "color:black;"
#                         "text-shadow:0 0 3px white;'>"
#                         "X</div>"
#                     ),
#                     "className": "dz-entry-x-marker",
#                     "iconSize": [22, 22],
#                     "iconAnchor": [11, 11],
#                 },
#                 children=[
#                     dl.Tooltip(
#                         [
#                             html.Div(
#                                 "First DZ point in interaction"
#                             ),
#                             html.Div(
#                                 f"DZ: {row.MACHINE}"
#                             ),
#                             html.Div(
#                                 f"HOV: {row.PAIR_MACHINE}"
#                             ),
#                             html.Div(
#                                 f"Time: {row.TIMESTAMP_STR}"
#                             ),
#                             html.Div(
#                                 f"Distance: "
#                                 f"{row.DISTANCE_METRES:.2f} m"
#                             ),
#                             html.Div(
#                                 f"Interaction: "
#                                 f"{row.INTERACTION_TYPE}"
#                             ),
#                             html.Div(
#                                 f"Moving: "
#                                 f"{row.MOVING_STATUS}"
#                             ),
#                             html.Div(
#                                 f"Area: "
#                                 f"{row.AREA_CATEGORY}"
#                             ),
#                         ]
#                     )
#                 ],
#             )
#         )

#     return layers


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, port=8002)