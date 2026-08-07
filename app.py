# -*- coding: utf-8 -*-
"""
Streamlit app - איסוף נתוני GPS היסטוריים של נסיעות אוטובוס
מ-Open Bus Stride API. עובד מכל דפדפן, כולל טלפון.

הרצה מקומית: streamlit run app.py
פריסה אונליין: ראה הוראות בסוף הקובץ (בתגובה).
"""

import streamlit as st
import requests
import sqlite3
import json
import time
import io
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://open-bus-stride-api.hasadna.org.il"
REQUEST_TIMEOUT = 20
RETRY_COUNT = 3

st.set_page_config(page_title="איסוף GPS אוטובוסים", layout="centered")


# ============================================================
# HTTP helper
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def api_get(path, params_tuple):
    params = dict(params_tuple)
    url = f"{BASE_URL}{path}"
    last_err = None
    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"נכשל GET {url} params={params}: {last_err}")


def api_get_live(path, params):
    """גרסה בלי cache, לשימוש בתוך threads בזמן איסוף בפועל."""
    url = f"{BASE_URL}{path}"
    last_err = None
    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"נכשל GET {url} params={params}: {last_err}")


# ============================================================
# שליפת רשימות לבחירה
# ============================================================

def list_operators():
    data = api_get("/gtfs_agencies/list", (("limit", 200),))
    # יש רשומות היסטוריות כפולות לאותו operator_ref (למשל "אגד" מופיע כמה פעמים
    # עם תאריכי תוקף שונים) - שומרים רק ערך ייחודי אחד לכל operator_id
    seen_ids = {}
    for d in data:
        op_id = d.get("operator_ref")
        name = d.get("agency_name")
        if op_id is None:
            continue
        if op_id not in seen_ids:
            seen_ids[op_id] = name
    ops = [{"operator_id": op_id, "name": name} for op_id, name in seen_ids.items()]
    ops.sort(key=lambda o: o["name"] or "")
    return ops


def list_routes(operator_id, line_number, days_back, filter_by_line=True):
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")
    # לא שולחים line_refs ל-API: זהו מזהה פנימי בבסיס הנתונים ולא מספר הקו הציבורי
    # (route_short_name). מספר קו אחד (למשל 826) יכול להופיע עם כמה line_ref שונים.
    # לכן שולפים לפי מפעיל+תאריכים בלבד, ומסננים בעצמנו לפי route_short_name.
    params = (
        ("operator_refs", str(operator_id)),
        ("date_from", date_from),
        ("date_to", date_to),
        ("limit", 1000),
    )
    data = api_get("/gtfs_routes/list", params)

    routes = []
    for d in data:
        short_name = str(d.get("route_short_name", "")).strip()
        line_ref = str(d.get("line_ref", "")).strip()
        if filter_by_line and short_name != str(line_number).strip():
            continue
        route_key = f'{d.get("line_ref")}-{d.get("route_mkt")}-#' if d.get("route_mkt") else d.get("route_short_name")

        # route_long_name בדרך כלל בפורמט "מוצא<->יעד" - הופכים לתווית קריאה יותר
        long_name = d.get("route_long_name") or ""
        if "<->" in long_name:
            origin, _, dest = long_name.partition("<->")
            direction_label = f"{origin.strip()}  ⬅➡  {dest.strip()}"
        else:
            direction_label = long_name

        routes.append({
            "route_key": route_key,
            "line_ref": line_ref,
            "label": f'קו {short_name} | {direction_label} | תוקף: {d.get("date")}',
            "raw": d,
        })
    # הסרת כפילויות (אותו route_key יכול לחזור על כמה תאריכים)
    seen = set()
    uniq = []
    for r in routes:
        if r["route_key"] not in seen:
            seen.add(r["route_key"])
            uniq.append(r)
    return uniq


def list_departure_times(operator_id, line_number, route_key, sample_date):
    """מביא את כל שעות היציאה המתוכננות בפועל (מה-SIRI) עבור מסלול נתון ביום דוגמה."""
    date_str = sample_date.strftime("%Y-%m-%d")
    params = {
        "gtfs_route__route_short_name": line_number,
        "gtfs_route__operator_refs": str(operator_id),
        "scheduled_start_time_from": f"{date_str}T00:00:00",
        "scheduled_start_time_to": f"{date_str}T23:59:59",
        "order_by": "scheduled_start_time asc",
        "limit": 500,
    }
    if route_key:
        params["gtfs_route__route_key"] = route_key
    rides = api_get_live("/siri_rides/list", params)
    times = sorted({r.get("scheduled_start_time", "")[11:16] for r in rides if r.get("scheduled_start_time")})
    return [t for t in times if t]


# ============================================================
# איסוף
# ============================================================

def get_ride_for_date_time(operator_id, line_number, route_key, target_date, departure_time):
    date_str = target_date.strftime("%Y-%m-%d")
    params = {
        "gtfs_route__route_short_name": line_number,
        "gtfs_route__operator_refs": str(operator_id),
        "scheduled_start_time_from": f"{date_str}T00:00:00",
        "scheduled_start_time_to": f"{date_str}T23:59:59",
        "order_by": "scheduled_start_time asc",
        "limit": 200,
    }
    if route_key:
        params["gtfs_route__route_key"] = route_key

    rides = api_get_live("/siri_rides/list", params)
    for r in rides:
        sched = r.get("scheduled_start_time")
        if sched and sched[11:16] == departure_time:
            return {"siri_ride_id": r.get("id"), "scheduled_start_time": sched}
    return None


def get_gps_samples(siri_ride_id):
    params = {"siri_ride_id": siri_ride_id, "order_by": "recorded_at_time asc", "limit": 5000}
    data = api_get_live("/siri_vehicle_locations/list", params)
    return [{"time": d.get("recorded_at_time"), "lat": d.get("lat"), "lon": d.get("lon")} for d in data]


def collect_one(operator_id, line_number, route_key, target_date, departure_time):
    date_str = target_date.strftime("%Y-%m-%d")
    try:
        ride = get_ride_for_date_time(operator_id, line_number, route_key, target_date, departure_time)
        if ride is None:
            return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                     "status": "no_ride_found", "siri_ride_id": None, "gps_count": 0, "gps_samples": None}
        samples = get_gps_samples(ride["siri_ride_id"])
        return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                 "status": "ok" if samples else "ride_found_no_gps",
                 "siri_ride_id": ride["siri_ride_id"], "gps_count": len(samples),
                 "gps_samples": json.dumps(samples, ensure_ascii=False)}
    except Exception as e:
        return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                 "status": f"error: {e}", "siri_ride_id": None, "gps_count": 0, "gps_samples": None}


def build_sqlite_bytes(rows):
    buf = io.BytesIO()
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE bus_rides (
            line TEXT, date TEXT, scheduled_time TEXT, status TEXT,
            siri_ride_id INTEGER, gps_count INTEGER, gps_samples TEXT,
            PRIMARY KEY (line, date, scheduled_time)
        )
    """)
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO bus_rides VALUES (?,?,?,?,?,?,?)",
            (row["line"], row["date"], row["scheduled_time"], row["status"],
             row["siri_ride_id"], row["gps_count"], row["gps_samples"]),
        )
    conn.commit()
    for line in conn.iterdump():
        buf.write((line + "\n").encode("utf-8"))
    conn.close()
    buf.seek(0)
    return buf.read()


# ============================================================
# UI
# ============================================================

st.title("🚌 איסוף נתוני GPS היסטוריים")
st.caption("Open Bus Stride API · הנדסה הפוכה של נתוני SIRI היסטוריים")

with st.spinner("טוען רשימת מפעילים..."):
    try:
        operators = list_operators()
    except Exception as e:
        st.error(f"שגיאה בטעינת מפעילים: {e}")
        operators = []

if operators:
    op_labels = [f'{o["name"]} ({o["operator_id"]})' for o in operators]
    op_idx = st.selectbox("מפעיל", range(len(operators)), format_func=lambda i: op_labels[i])
    operator_id = operators[op_idx]["operator_id"]
else:
    operator_id = st.text_input("מזהה מפעיל (operator_id)", value="3")

line_number = st.text_input("מספר קו", value="836")
days_back = st.number_input("כמה ימים אחורה", min_value=1, max_value=180, value=28)

debug_no_filter = st.checkbox("🐞 מצב דיבאג: הצג את כל התוצאות בלי סינון לפי מספר קו", value=False)

route_key = None
if st.button("🔍 טען מסלולים אפשריים לקו זה"):
    with st.spinner("טוען מסלולים..."):
        try:
            routes = list_routes(operator_id, line_number, days_back, filter_by_line=not debug_no_filter)
            st.session_state["routes"] = routes
            if not routes:
                st.warning("לא נמצאו מסלולים כלל בטווח התאריכים שנבחר (גם בלי סינון). ייתכן שאין דאטה זמין לקו/למפעיל/לתאריכים האלה.")
            elif debug_no_filter:
                st.info(f"נמצאו {len(routes)} תוצאות ללא סינון - תוכל לבדוק אילו שדות (line_ref/route_short_name) חוזרים בפועל מה-API.")
        except Exception as e:
            st.error(f"שגיאה בטעינת מסלולים: {e}")

if "routes" in st.session_state and st.session_state["routes"]:
    routes = st.session_state["routes"]
    r_idx = st.selectbox("מסלול (כיוון)", range(len(routes)), format_func=lambda i: routes[i]["label"])
    route_key = routes[r_idx]["route_key"]
    st.caption(f"route_key שנבחר: `{route_key}`")

    if st.button("🕐 טען שעות יציאה זמינות למסלול זה"):
        with st.spinner("טוען שעות יציאה מהימים האחרונים..."):
            try:
                # בודקים על יום אתמול (בד"כ יש יותר דאטה סופי מאשר על היום הנוכחי)
                sample_date = datetime.now().date() - timedelta(days=1)
                times = list_departure_times(operator_id, line_number, route_key, sample_date)
                if not times:
                    # fallback ליום נוסף אם אתמול לא היה יום פעיל
                    sample_date = datetime.now().date() - timedelta(days=2)
                    times = list_departure_times(operator_id, line_number, route_key, sample_date)
                st.session_state["available_times"] = times
                if not times:
                    st.warning("לא נמצאו שעות יציאה בימים האחרונים למסלול זה.")
            except Exception as e:
                st.error(f"שגיאה בטעינת שעות: {e}")

departure_times = []
if "available_times" in st.session_state and st.session_state["available_times"]:
    st.write("בחר שעות יציאה מהרשימה (מבוסס על נתונים אמיתיים):")
    cols = st.columns(4)
    for i, t in enumerate(st.session_state["available_times"]):
        with cols[i % 4]:
            if st.checkbox(t, key=f"time_{t}", value=(t in ("16:30", "16:55"))):
                departure_times.append(t)
else:
    st.caption("טען מסלול ואז שעות יציאה זמינות, או הזן ידנית למטה.")

departure_times_manual = st.text_input("או: הזן שעות יציאה ידנית (מופרדות בפסיק)", value="")
if departure_times_manual.strip():
    departure_times = [t.strip() for t in departure_times_manual.split(",") if t.strip()]

max_workers = st.slider("מקביליות (מס' בקשות במקביל)", 1, 16, 8)

st.divider()

if st.button("▶️ התחל איסוף", type="primary", disabled=not (operator_id and line_number and departure_times)):
    today = datetime.now().date()
    start_date = today - timedelta(days=int(days_back))
    all_dates = [start_date + timedelta(days=i) for i in range((today - start_date).days)]
    tasks = [(d, t) for d in all_dates for t in departure_times]

    st.write(f"סה״כ {len(tasks)} נסיעות לבדיקה ({len(all_dates)} ימים × {len(departure_times)} שעות יציאה)")

    progress = st.progress(0)
    status_area = st.empty()
    rows = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect_one, operator_id, line_number, route_key, d, t): (d, t)
            for d, t in tasks
        }
        done = 0
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            done += 1
            progress.progress(done / len(tasks))
            status_area.text(f"[{done}/{len(tasks)}] {row['date']} {row['scheduled_time']} -> {row['status']} ({row['gps_count']} דגימות)")

    st.success("האיסוף הסתיים!")
    st.session_state["collected_rows"] = rows

if "collected_rows" in st.session_state:
    rows = st.session_state["collected_rows"]
    ok_rows = [r for r in rows if r["status"] == "ok"]
    st.metric("נסיעות עם GPS", f"{len(ok_rows)} / {len(rows)}")

    st.dataframe(
        [{"תאריך": r["date"], "שעה": r["scheduled_time"], "סטטוס": r["status"], "דגימות GPS": r["gps_count"]} for r in rows],
        use_container_width=True,
    )

    sqlite_bytes = build_sqlite_bytes(rows)
    st.download_button("⬇️ הורד כקובץ SQLite", data=sqlite_bytes, file_name="bus_gps_data.sql", mime="text/plain")

    import csv
    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=["line", "date", "scheduled_time", "status", "siri_ride_id", "gps_count", "gps_samples"])
    writer.writeheader()
    writer.writerows(rows)
    st.download_button("⬇️ הורד כקובץ CSV", data=csv_buf.getvalue().encode("utf-8"), file_name="bus_gps_data.csv", mime="text/csv")