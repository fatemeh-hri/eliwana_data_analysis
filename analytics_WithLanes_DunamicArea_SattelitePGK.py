import pandas as pd
import geopandas as gpd
from shapely import wkt
import plotly.graph_objects as go
import snowflake.connector
import os
from dotenv import load_dotenv
import random
import ezdxf
from shapely.geometry import LineString

from dash import Dash, dcc, html
from dash.dependencies import Input, Output

import plotly.io as pio
pio.templates["plotly"].layout.mapbox.accesstoken = "pk.eyJ1IjoiZmF0ZW1laC1ocmkiLCJhIjoiY21wZGppbXk4MDI5NTJyb29za21tMHczdSJ9.rbe6zVOvEtIlWI-9SpEu7A"

DXF_PATH = r"Map_Lane/FMS_Map_20260520_135053.dxf"

SOURCE_CRS = "EPSG:28350"
TARGET_CRS = "EPSG:4326"

DEFAULT_START_DATETIME = "2026-05-09 07:19:01"
DEFAULT_END_DATETIME = "2026-05-09 08:19:01"

# -----------------------------
# Helper Function
# -----------------------------
def clean_datetime(value, default_value):
    if value is None or str(value).strip() == "":
        return default_value

    return str(value).strip()
# -----------------------------
# DXF LOADER
# -----------------------------
def load_dxf_as_single_trace(dxf_path):
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

    lon = []
    lat = []

    for geom in dxf_gdf.geometry:
        x, y = geom.xy
        lon.extend(list(x) + [None])
        lat.extend(list(y) + [None])

    return go.Scattermapbox(
        lon=lon,
        lat=lat,
        mode="lines",
        line=dict(color="white", width=1),
        opacity=0.5,
        name="DXF Map",
        hoverinfo="skip",
        showlegend=False
    )


# -----------------------------
# SNOWFLAKE DATA LOADER
# -----------------------------
def load_nearby_machine_data(start_date, end_date, max_distance):
    load_dotenv()
    conn = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            authenticator=os.getenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
            role=os.environ["SNOWFLAKE_ROLE"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=os.environ["SNOWFLAKE_DATABASE"],
            schema=os.environ["SNOWFLAKE_SCHEMA"],
        )

    query = f"""
            WITH hme AS (
                SELECT *
                FROM AA_OPERATIONS_MANAGEMENT.SELFSERVICE.FMS_MACHINE_LOCATION
                WHERE "TIMESTAMP" BETWEEN '{start_date}' AND '{end_date}'
                AND LEFT(MACHINE, 2) IN ('DZ', 'WL', 'WD')
            ),

            lv AS (
                SELECT *
                FROM AA_OPERATIONS_MANAGEMENT.SELFSERVICE.FMS_MACHINE_LOCATION
                WHERE "TIMESTAMP" BETWEEN '{start_date}' AND '{end_date}'
                AND LEFT(MACHINE, 2) = 'EL'
            ),

            matches AS (
                SELECT
                    h.MACHINE AS HME_MACHINE,
                    l.MACHINE AS LV_MACHINE,
                    LEFT(h.MACHINE, 2) AS HME_PREFIX,
                    LEFT(l.MACHINE, 2) AS LV_PREFIX,
                    h."TIMESTAMP",
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

                    ROW_NUMBER() OVER (
                        PARTITION BY h.MACHINE, h."TIMESTAMP"
                        ORDER BY ST_DISTANCE(h.THE_GEOM, l.THE_GEOM)
                    ) AS rn
                FROM hme h
                JOIN lv l
                ON h."TIMESTAMP" = l."TIMESTAMP"
                AND ST_DISTANCE(h.THE_GEOM, l.THE_GEOM) <= {max_distance}
            )

            SELECT *
            FROM matches
            WHERE rn = 1
            ORDER BY "TIMESTAMP";
    """

    df = pd.read_sql(query, conn)
    conn.close()

    return df



# -----------------------------
# PROCESS HME-LV MATCH DATA FOR MAP
# -----------------------------
def process_pair_data(df):
    if df.empty:
        return gpd.GeoDataFrame()

    df["TIMESTAMP"] = (
        pd.to_datetime(df["TIMESTAMP"], utc=True)
          .dt.tz_convert("Australia/Perth")
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

    gdf = gpd.GeoDataFrame(plot_df, geometry="geometry", crs=SOURCE_CRS)
    gdf = gdf.to_crs(TARGET_CRS)

    gdf.sort_values(["TYPE", "PREFIX", "TIMESTAMP"], inplace=True)

    gdf["lon"] = gdf.geometry.x
    gdf["lat"] = gdf.geometry.y
    gdf["TIMESTAMP_STR"] = gdf["TIMESTAMP"].astype(str)

    gdf["TIME_INT"] = (
                gdf["TIMESTAMP"] - gdf["TIMESTAMP"].min()
            ).dt.total_seconds().astype(int)

    return gdf


# -----------------------------
# LOAD STATIC DXF
# -----------------------------
dxf_trace = load_dxf_as_single_trace(DXF_PATH)


# -----------------------------
# DASH APP
# -----------------------------
app = Dash(__name__)

app.layout = html.Div(
    style={"height": "100vh"},
    children=[

        html.Div(
            "HaulX – Nearby Machine Detection",
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
                    style={"marginRight": "20px", "width": "180px"}
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


        html.Div(
            style={"padding": "10px"},
            children=[
                html.Label("Distance Threshold"),

                dcc.Dropdown(
                    id="distance-dropdown",
                    options=[
                        {"label": "10 m", "value": 10},
                        {"label": "20 m", "value": 20},
                        {"label": "50 m", "value": 50},
                    ],
                    value=50,
                    clearable=False
                )
            ]
        ),

        html.Div(
            style={"padding": "10px"},
            children=[
                html.Label("Select Machine(s)"),

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

        dcc.Graph(
            id="trajectory-map",
            style={"height": "80vh"}
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
    Input("distance-dropdown", "value")
)
def update_controls(start_date, end_date, distance_threshold):
    
    start_date = clean_datetime(start_date, DEFAULT_START_DATETIME)
    end_date = clean_datetime(end_date, DEFAULT_END_DATETIME)
    df = load_nearby_machine_data(
        start_date=start_date,
        end_date=end_date,
        max_distance=distance_threshold
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
    Output("trajectory-map", "figure"),

    Input("machine-dropdown", "value"),
    Input("time-slider", "value"),
    Input("distance-dropdown", "value"),
    Input("start-datetime", "value"),
    Input("end-datetime", "value")
)
def update_map(selected_machines, time_limit, distance_threshold, start_date, end_date):
    start_date = clean_datetime(start_date, DEFAULT_START_DATETIME)
    end_date = clean_datetime(end_date, DEFAULT_END_DATETIME)
    df = load_nearby_machine_data(
        start_date=start_date,
        end_date=end_date,
        max_distance=distance_threshold
    )

    gdf_plot = process_pair_data(df)

    traces = [dxf_trace]

    if gdf_plot.empty:
        fig = go.Figure(data=traces)
        fig.update_layout(
            mapbox=dict(
                style="satellite-streets",
                center=dict(lat=-22.0, lon=119.0),
                zoom=10
            ),
            margin=dict(l=0, r=0, t=0, b=0)
        )
        return fig

    gdf_plot = gdf_plot[
            (
                ((gdf_plot["TYPE"] == "HME") & gdf_plot["PREFIX"].isin(selected_machines)) |
                ((gdf_plot["TYPE"] == "LV") & gdf_plot["PAIR_PREFIX"].isin(selected_machines))
            ) &
            (gdf_plot["TIME_INT"] <= time_limit) &
            (gdf_plot["DISTANCE_METRES"] <= distance_threshold)
        ]

    grouped = {
            (machine_type, prefix): g.sort_values("TIME_INT")
            for (machine_type, prefix), g in gdf_plot.groupby(["TYPE", "PREFIX"])
        }


    for (machine_type, prefix), group in grouped.items():

        group = group.dropna(subset=["lon", "lat"])

        if group.empty:
            continue

        if machine_type == "HME":
            mode = "markers+text"
            text = ["X"] * len(group)
            textfont = dict(size=20, color="black")
            marker = dict(
                size=8,
                color="red",
                # opacity=0.3
            )
            trace_name = f"{prefix} HME within {distance_threshold}m of LV"
        else:
            mode = "markers"
            text = None
            textfont = None
            marker = dict(
                size=8,
                color="yellow",
                opacity=0.8
            )
            trace_name = f"{prefix} LV near HME"

        traces.append(
            go.Scattermapbox(
                lon=group["lon"],
                lat=group["lat"],
                mode=mode,
                text=text,
                textposition="middle center",
                textfont=textfont,
                marker=marker,
                name=trace_name,

                customdata=group[[
                    "TYPE",
                    "PREFIX",
                    "MACHINE",
                    "PAIR_PREFIX",
                    "PAIR_MACHINE",
                    "TIMESTAMP_STR",
                    "DISTANCE_METRES",
                    "X",
                    "Y"
                ]],

                hovertemplate=
                    "<b>Type:</b> %{customdata[0]}<br>" +
                    "<b>Prefix:</b> %{customdata[1]}<br>" +
                    "<b>Machine:</b> %{customdata[2]}<br>" +
                    "<b>Near Prefix:</b> %{customdata[3]}<br>" +
                    "<b>Near Machine:</b> %{customdata[4]}<br>" +
                    "<b>Time:</b> %{customdata[5]}<br>" +
                    "<b>Distance:</b> %{customdata[6]:.2f} m<br>" +
                    "<b>X:</b> %{customdata[7]:.2f}<br>" +
                    "<b>Y:</b> %{customdata[8]:.2f}<br>" +
                    "<extra></extra>"
            )
        )

    fig = go.Figure(data=traces)

    fig.update_layout(
        mapbox=dict(
            style="satellite-streets",
            center=dict(
                lat=gdf_plot["lat"].mean(),
                lon=gdf_plot["lon"].mean()
            ),
            zoom=16
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        legend=dict(title="Nearby Machines")
    )

    return fig


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, port=8000)