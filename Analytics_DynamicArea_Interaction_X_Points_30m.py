import pandas as pd
import geopandas as gpd
from shapely import wkt
import snowflake.connector
from sqlalchemy import create_engine
import ezdxf
import os
from dotenv import load_dotenv
from shapely.geometry import LineString
from lxml import etree

from dash import Dash, dcc, html
from dash.dependencies import Input, Output
import dash_leaflet as dl


# -----------------------------
# CONFIG
# -----------------------------
DXF_PATH = r"Map_Lane/FMS_Map_20260520_135053.dxf"
KML_FILE = r"Map_Layer/ELI_Area.kml"
WMS_URL = "https://fortescuesky.fmgl.com.au/SG/default/streamer.ashx?"

SOURCE_CRS = "EPSG:28350"
TARGET_CRS = "EPSG:4326"

DEFAULT_START_DATETIME = "2026-05-09 11:19:01"
DEFAULT_END_DATETIME = "2026-05-09 13:19:01"

load_dotenv()

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

    lines = []

    for e in msp.query("LWPOLYLINE"):
        layer_name = e.dxf.layer.upper()

        if "LANES" not in layer_name:
            continue

        pts = [(p[0], p[1]) for p in e.get_points()]

        if len(pts) >= 2:
            lines.append(LineString(pts))

    dxf_gdf = gpd.GeoDataFrame(geometry=lines, crs=SOURCE_CRS)
    dxf_gdf = dxf_gdf.to_crs(TARGET_CRS)

    dxf_gdf["geometry"] = dxf_gdf.geometry.simplify(
        0.000003,
        preserve_topology=True
    )

    return dxf_gdf


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
            ),

            movement AS (
                SELECT
                    *,
                    CASE
                        WHEN PREV_X IS NULL THEN NULL
                        WHEN SQRT(
                            POWER(X - PREV_X, 2) +
                            POWER(Y - PREV_Y, 2) +
                            POWER(Z - PREV_Z, 2)
                        ) > 0.5 THEN 1
                        ELSE 0
                    END AS IS_MOVING
                FROM base
            ),

            hme AS (
                SELECT *
                FROM movement
                WHERE LEFT(MACHINE, 2) IN ('DZ', 'WL', 'WD', 'DT')
            ),

            lv AS (
                SELECT *
                FROM movement
                WHERE LEFT(MACHINE, 2) = 'EL'
            ),

            matches AS (
                SELECT
                    h.MACHINE AS HME_MACHINE,
                    l.MACHINE AS LV_MACHINE,

                    LEFT(h.MACHINE, 2) AS HME_PREFIX,
                    LEFT(l.MACHINE, 2) AS LV_PREFIX,

                    TO_CHAR(h.TS_SECOND, 'YYYY-MM-DD HH24:MI:SS') AS "TIMESTAMP",

                    ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) AS DISTANCE_METRES,

                    h.X AS HME_X,
                    h.Y AS HME_Y,
                    h.Z AS HME_Z,
                    h.WKT_GEOM AS HME_WKT_GEOM,
                    h.THE_GEOM AS HME_GEOM,

                    l.X AS LV_X,
                    l.Y AS LV_Y,
                    l.Z AS LV_Z,
                    l.WKT_GEOM AS LV_WKT_GEOM,
                    l.THE_GEOM AS LV_GEOM,

                    h.IS_MOVING AS HME_IS_MOVING,
                    l.IS_MOVING AS LV_IS_MOVING,

                    ROW_NUMBER() OVER (
                        PARTITION BY h.MACHINE, h.TS_SECOND
                        ORDER BY ST_DISTANCE(h.THE_GEOM, l.THE_GEOM)
                    ) AS rn

                FROM hme h
                JOIN lv l
                    ON h.TS_SECOND = l.TS_SECOND
                    AND ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) <= 30

                WHERE h.IS_MOVING = 1
            )

            SELECT *
            FROM matches
            WHERE rn = 1
            ORDER BY "TIMESTAMP";
    """


    df = pd.read_sql(query, engine.connect()).rename(columns=str.upper)

    return df


# -----------------------------
# PROCESS HME-LV MATCH DATA
# -----------------------------
def process_pair_data(df):
    if df.empty:
        return gpd.GeoDataFrame()

    df["TIMESTAMP"] = pd.to_datetime(
            df["TIMESTAMP"],
            format="%Y-%m-%d %H:%M:%S"
        )
    df["HME_GEOMETRY"] = df["HME_WKT_GEOM"].apply(wkt.loads)
    df["LV_GEOMETRY"] = df["LV_WKT_GEOM"].apply(wkt.loads)

    hme_df = df[[
        "HME_MACHINE",
        "HME_PREFIX",
        "LV_MACHINE",
        "LV_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "HME_X",
        "HME_Y",
        "HME_Z",
        "HME_GEOMETRY"
    ]].copy()

    hme_df.columns = [
        "MACHINE",
        "PREFIX",
        "PAIR_MACHINE",
        "PAIR_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "X",
        "Y",
        "Z",
        "geometry"
    ]

    hme_df["TYPE"] = "HME"

    lv_df = df[[
        "LV_MACHINE",
        "LV_PREFIX",
        "HME_MACHINE",
        "HME_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "LV_X",
        "LV_Y",
        "LV_Z",
        "LV_GEOMETRY"
    ]].copy()

    lv_df.columns = [
        "MACHINE",
        "PREFIX",
        "PAIR_MACHINE",
        "PAIR_PREFIX",
        "TIMESTAMP",
        "DISTANCE_METRES",
        "X",
        "Y",
        "Z",
        "geometry"
    ]

    lv_df["TYPE"] = "LV"

    plot_df = pd.concat([hme_df, lv_df], ignore_index=True)

    plot_df = plot_df.drop_duplicates(
        subset=["TYPE", "PREFIX", "MACHINE", "TIMESTAMP", "X", "Y"]
    )

    gdf = gpd.GeoDataFrame(
        plot_df,
        geometry="geometry",
        crs=SOURCE_CRS
    )

    gdf = gdf.to_crs(TARGET_CRS)

    gdf.sort_values(["TYPE", "PREFIX", "TIMESTAMP"], inplace=True)

    gdf["lon"] = gdf.geometry.x
    gdf["lat"] = gdf.geometry.y
    gdf["TIMESTAMP_STR"] = gdf["TIMESTAMP"].astype(str).str.split("+").str[0]

    gdf["TIME_INT"] = (
        gdf["TIMESTAMP"] - gdf["TIMESTAMP"].min()
    ).dt.total_seconds().astype(int)

    return gdf


# -----------------------------
# LOAD STATIC MAP DATA
# -----------------------------
geojson_data = kml_to_geojson(KML_FILE)
dxf_gdf = load_dxf_lanes(DXF_PATH)

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


# -----------------------------
# DASH APP
# -----------------------------
app = Dash(__name__)

app.layout = html.Div(
    style={"height": "100vh"},
    children=[

        html.Div(
            "Eliwana Dynamic Area – HME/LV Interactions Within 30 m",
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
                html.Label("Select Machine Prefix(es)"),

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
                html.Label("Time"),
                dcc.Slider(
                    id="time-slider",
                    min=0,
                    max=1,
                    value=1,
                    marks={0: "Start", 1: "End"},
                    step=60
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

    df = load_nearby_machine_data(
        start_date=start_date,
        end_date=end_date,
        # max_distance=distance_threshold
    )

    gdf_plot = process_pair_data(df)

    if gdf_plot.empty:
        return [], [], 0, 1, 1, {0: "No data"}

    machines = sorted(
        gdf_plot.loc[gdf_plot["TYPE"] == "HME", "PREFIX"].unique()
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
        max_time,
        time_marks
    )


# -----------------------------
# UPDATE MAP
# -----------------------------
@app.callback(
    Output("dynamic-layers", "children"),

    Input("machine-dropdown", "value"),
    Input("time-slider", "value"),
    # Input("distance-dropdown", "value"),
    Input("start-datetime", "value"),
    Input("end-datetime", "value")
)
def update_map(selected_machines, time_limit, start_date, end_date):
    prefix_colors = {
            "DT": "red",
            "DZ": "blue",
            "WD": "green",
            "WL": "purple",
        }
    if not selected_machines:
        return []

    start_date = clean_datetime(start_date, DEFAULT_START_DATETIME)
    end_date = clean_datetime(end_date, DEFAULT_END_DATETIME)

    df = load_nearby_machine_data(
        start_date=start_date,
        end_date=end_date,
        # max_distance=distance_threshold
    )
    print('Snowflake data loaded, processing for map update...')
    gdf_plot = process_pair_data(df)

    if gdf_plot.empty:
        return []

    gdf_plot = gdf_plot[
        (
            (
                (gdf_plot["TYPE"] == "HME") &
                (gdf_plot["PREFIX"].isin(selected_machines))
            ) |
            (
                (gdf_plot["TYPE"] == "LV") &
                (gdf_plot["PAIR_PREFIX"].isin(selected_machines))
            )
        ) &
        (gdf_plot["TIME_INT"] <= time_limit) 
        # (gdf_plot["DISTANCE_METRES"] <= distance_threshold)
        ]
    

    hme_points = (
    gdf_plot[gdf_plot["TYPE"] == "HME"]
        .dropna(subset=["lon", "lat"])
        .sort_values(["MACHINE", "PAIR_MACHINE", "TIME_INT"])
        .copy()
    )

    # New CAS interaction starts when same HME/LV pair has a time gap

    MAX_GAP_SECONDS = 5

    hme_points["PREV_TIME_INT"] = (
        hme_points
        .groupby(["MACHINE", "PAIR_MACHINE"])["TIME_INT"]
        .shift()
    )

    hme_points["NEW_INTERACTION"] = (
        hme_points["PREV_TIME_INT"].isna() |
        ((hme_points["TIME_INT"] - hme_points["PREV_TIME_INT"]) > MAX_GAP_SECONDS)
    )

    hme_points["INTERACTION_ID"] = (
        hme_points
        .groupby(["MACHINE", "PAIR_MACHINE"])["NEW_INTERACTION"]
        .cumsum()
    )

    first_hme_points = (
        hme_points
        .sort_values(["MACHINE", "PAIR_MACHINE", "INTERACTION_ID", "TIME_INT"])
        .drop_duplicates(
            subset=["MACHINE", "PAIR_MACHINE", "INTERACTION_ID"],
            keep="first"
        )
    )
    if gdf_plot.empty:
        return []

    layers = []
    
    grouped = {
        (machine_type, prefix): group.sort_values("TIME_INT")
        for (machine_type, prefix), group in gdf_plot.groupby(["TYPE", "PREFIX"])
    }


    for machine_type, group in grouped.items():
        
        group = group.dropna(subset=["lon", "lat"])

        if group.empty:
            continue


        for _, row in group.iterrows():
            if row["TYPE"] == "HME":

                marker_color = prefix_colors.get(row["PREFIX"], "gray")
            else:
                marker_color = "yellow"

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
                        html.Div(f"Machine: {row['PREFIX']}"),
                        html.Div(f"Near Machine: {row['PAIR_PREFIX']}"),
                        html.Div(f"Time: {row['TIMESTAMP']}"),
                        html.Div(f"Distance: {row['DISTANCE_METRES']:.2f} m"),
                        html.Div(f"X: {row['X']:.2f}"),
                        html.Div(f"Y: {row['Y']:.2f}")
                    ])
                ]
            )
    )

        for _, value in first_hme_points.iterrows():
            layers.append(
                dl.DivMarker(
                    id=(
                        f"FIRST_HME_X_"
                        f"{value['MACHINE']}_"
                        f"{value['PAIR_MACHINE']}_"
                        f"{value['TIME_INT']}"
                    ),
                    position=[value["lat"], value["lon"]],
                    iconOptions={
                            "html": "<div style='font-size:22px;font-weight:bold;color:black;text-shadow:0 0 3px white;'>X</div>",
                            "className": "hme-entry-x-marker",
                            "iconSize": [22, 22]
                        },
                    children=[
                        dl.Tooltip([
                            html.Div("First HME within CAS range"),
                            html.Div(f"HME: {value['MACHINE']}"),
                            html.Div(f"LV: {value['PAIR_MACHINE']}"),
                            html.Div(f"Time: {value['TIMESTAMP']}"),
                            html.Div(f"Distance: {value['DISTANCE_METRES']:.2f} m"),
                            html.Div(f"X: {value['X']:.2f}"),
                            html.Div(f"Y: {value['Y']:.2f}")
                        ])
                    ]
                )
            )   
    return layers


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, port=8001)