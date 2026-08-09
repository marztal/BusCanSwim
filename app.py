# -*- coding: utf-8 -*-
"""
Streamlit app - איסוף נתוני GPS היסטוריים של נסיעות אוטובוס
מ-Open Bus Stride API. עובד מכל דפדפן, כולל טלפון.

בנוי לפי הזרימה המוכחת שעבדה בעבר עבור קו 826 (route826.py):
  1. gtfs_routes/list (route_short_name)          -> gtfs_route_id לכל תאריך+כיוון
  2. gtfs_rides/list (gtfs_route_id)               -> לוח זמנים מתוכנן (start_time)
  3. siri_routes/list (line_refs)                  -> siri_route_id לפי הכיוון
  4. siri_ride_stops/list (siri_route__line_ref)   -> siri_ride_id + vehicle_ref
  5. siri_vehicle_locations/list (siri_rides__ids) -> כל דגימות ה-GPS

הרצה מקומית: streamlit run app.py
"""

import streamlit as st
import requests
import sqlite3
import json
import time
import io
import csv
from urllib.parse import urlencode, quote
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://open-bus-stride-api.hasadna.org.il"
REQUEST_TIMEOUT = 45
RETRY_COUNT = 4
IL_OFFSET = timedelta(hours=3)  # קירוב לשעון ישראל (לא מטפל ב-DST בצורה מדויקת)

st.set_page_config(page_title="איסוף GPS אוטובוסים", layout="centered")


# ============================================================
# HTTP helper
# ============================================================

def fetch(endpoint, params, timeout=REQUEST_TIMEOUT):
    """קריאת GET גנרית, עם retry על 500 (בדיוק כמו בסקריפט המקורי שעבד)."""
    q = urlencode(params, quote_via=quote)
    url = f"{BASE_URL}/{endpoint}?{q}"
    last_err = None
    for attempt in range(RETRY_COUNT):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 500:
                last_err = f"500 Server Error (ניסיון {attempt+1})"
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except requests.exceptions.Timeout:
            last_err = "timeout"
        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"נכשל GET {endpoint} params={params}: {last_err}")


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_cached(endpoint, params_tuple):
    return fetch(endpoint, dict(params_tuple))


def il_hm(utc_str):
    if not utc_str:
        return ""
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return (dt + IL_OFFSET).strftime("%H:%M")


# ============================================================
# שלב 1: מפעילים
# ============================================================

def list_operators():
    data = fetch_cached("gtfs_agencies/list", (("limit", 200),))
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


# ============================================================
# שלב 2: מסלולים (כיוונים) לקו נתון
# ============================================================

def list_routes(operator_id, line_number, days_back):
    """מביא את כל רשומות ה-gtfs_route של הקו הזה (route_short_name ישיר, בלי
    prefix - זה עובד ישירות על טבלת gtfs_route, לא דרך join), ומקבץ לפי כיוון
    (line_ref+route_direction) כדי להציג רשימת "מסלולים" ברורה לבחירה.
    """
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")
    rows = fetch("gtfs_routes/list", {
        "route_short_name": str(line_number),
        "operator_refs": str(operator_id),
        "date_from": date_from,
        "date_to": date_to,
        "order_by": "date desc",
        "limit": 1000,
    })

    # מקבצים לפי כיוון (line_ref + route_direction) - זו "הזהות" האמיתית של
    # המסלול; לכל כיוון שומרים את הרשומה העדכנית ביותר לתצוגה
    by_direction = {}
    for d in rows:
        key = (d.get("line_ref"), d.get("route_direction"))
        if key not in by_direction or d.get("date", "") > by_direction[key]["date"]:
            by_direction[key] = d

    routes = []
    for (line_ref, direction), d in by_direction.items():
        long_name = d.get("route_long_name") or ""
        if "<->" in long_name:
            origin, _, dest = long_name.partition("<->")
            direction_label = f"{origin.strip()}  ⬅➡  {dest.strip()}"
        else:
            direction_label = long_name
        routes.append({
            "line_ref": line_ref,
            "route_direction": direction,
            "label": f'קו {d.get("route_short_name")} | {direction_label} | כיוון: {direction} | עדכני ל: {d.get("date")}',
        })
    routes.sort(key=lambda r: str(r["route_direction"]))
    return routes


# ============================================================
# שלב 3: gtfs_route_id ליום ספציפי + כיוון נתון
# ============================================================

def get_gtfs_route_id_for_date(operator_id, line_number, line_ref, route_direction, date_str):
    """שולף gtfs_route_id התואם בדיוק לתאריך ולכיוון הנתונים."""
    rows = fetch("gtfs_routes/list", {
        "route_short_name": str(line_number),
        "operator_refs": str(operator_id),
        "date_from": date_str,
        "date_to": date_str,
        "limit": 200,
    })
    for r in rows:
        if r.get("line_ref") == line_ref and r.get("route_direction") == route_direction and r.get("date") == date_str:
            return r.get("id")
    return None


# ============================================================
# שלב 4: לוח זמנים מתוכנן (gtfs_rides) ליום נתון
# ============================================================

def get_gtfs_rides(gtfs_route_id):
    return fetch("gtfs_rides/list", {"gtfs_route_id": gtfs_route_id, "limit": 100})


# ============================================================
# שלב 5: siri_route_id לפי line_ref
# ============================================================

def get_siri_route_ids(line_ref):
    rows = fetch("siri_routes/list", {"line_refs": line_ref, "limit": 10})
    return [r["id"] for r in rows]


# ============================================================
# שלב 6: siri_ride_stops - התאמת siri_ride_id + vehicle_ref לפי scheduled_start_time
# ============================================================

def get_siri_data(line_ref, date_str):
    time_from = f"{date_str}T01:00:00+00:00"
    time_to = f"{date_str}T23:59:00+00:00"
    result = {}
    rows = fetch("siri_ride_stops/list", {
        "siri_route__line_ref": line_ref,
        "siri_ride__scheduled_start_time_from": time_from,
        "siri_ride__scheduled_start_time_to": time_to,
        "limit": 100,
    })
    for row in rows:
        key = row.get("siri_ride__scheduled_start_time", "")
        if key and key not in result:
            result[key] = row
    return result


# ============================================================
# שלב 7: GPS מלא לנסיעה ספציפית
# ============================================================

def get_gps_samples(siri_ride_id):
    rows = fetch("siri_vehicle_locations/list", {"siri_rides__ids": siri_ride_id, "limit": 5000})
    rows = sorted(rows, key=lambda r: r.get("recorded_at_time") or "")
    return [{"time": r.get("recorded_at_time"), "lat": r.get("lat"), "lon": r.get("lon")} for r in rows]


# ============================================================
# איסוף עבור יום+שעת יציאה בודדים
# ============================================================

def collect_one(operator_id, line_number, line_ref, route_direction, target_date, departure_time):
    date_str = target_date.strftime("%Y-%m-%d")
    try:
        gtfs_route_id = get_gtfs_route_id_for_date(operator_id, line_number, line_ref, route_direction, date_str)
        if gtfs_route_id is None:
            return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                     "status": "no_gtfs_route_for_date", "siri_ride_id": None, "gps_count": 0, "gps_samples": None}

        rides = get_gtfs_rides(gtfs_route_id)
        matching_ride = None
        for ride in rides:
            if il_hm(ride.get("start_time")) == departure_time:
                matching_ride = ride
                break
        if matching_ride is None:
            return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                     "status": "no_ride_at_this_time", "siri_ride_id": None, "gps_count": 0, "gps_samples": None}

        siri_data = get_siri_data(line_ref, date_str)
        siri_row = siri_data.get(matching_ride.get("start_time"), {})
        siri_ride_id = siri_row.get("siri_ride_id") or siri_row.get("siri_ride__id")
        if not siri_ride_id:
            return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                     "status": "no_siri_match", "siri_ride_id": None, "gps_count": 0, "gps_samples": None}

        samples = get_gps_samples(siri_ride_id)
        return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                 "status": "ok" if samples else "ride_found_no_gps",
                 "siri_ride_id": siri_ride_id, "gps_count": len(samples),
                 "gps_samples": json.dumps(samples, ensure_ascii=False)}
    except Exception as e:
        return {"line": line_number, "date": date_str, "scheduled_time": departure_time,
                 "status": f"error: {e}", "siri_ride_id": None, "gps_count": 0, "gps_samples": None}


def list_departure_times(operator_id, line_number, line_ref, route_direction, sample_date):
    """שעות יציאה מתוכננות אמיתיות ליום דוגמה, ישירות מ-gtfs_rides."""
    date_str = sample_date.strftime("%Y-%m-%d")
    gtfs_route_id = get_gtfs_route_id_for_date(operator_id, line_number, line_ref, route_direction, date_str)
    if gtfs_route_id is None:
        return [], gtfs_route_id
    rides = get_gtfs_rides(gtfs_route_id)
    times = sorted({il_hm(r.get("start_time")) for r in rides if r.get("start_time")})
    return [t for t in times if t], gtfs_route_id


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
st.caption("Open Bus Stride API · בנוי לפי הזרימה המוכחת מפרויקט קו 826")

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

line_ref = None
route_direction = None
if st.button("🔍 טען מסלולים (כיוונים) אפשריים לקו זה"):
    with st.spinner("טוען מסלולים..."):
        try:
            routes = list_routes(operator_id, line_number, days_back)
            st.session_state["routes"] = routes
            if not routes:
                st.warning("לא נמצאו מסלולים לקו/למפעיל/לתאריכים האלה.")
        except Exception as e:
            st.error(f"שגיאה בטעינת מסלולים: {e}")

if "routes" in st.session_state and st.session_state["routes"]:
    routes = st.session_state["routes"]
    r_idx = st.selectbox("מסלול (כיוון)", range(len(routes)), format_func=lambda i: routes[i]["label"])
    line_ref = routes[r_idx]["line_ref"]
    route_direction = routes[r_idx]["route_direction"]
    st.caption(f"line_ref: `{line_ref}` · route_direction: `{route_direction}`")

    if st.button("🕐 טען שעות יציאה זמינות למסלול זה"):
        with st.spinner("טוען שעות יציאה מהימים האחרונים..."):
            try:
                # מדלגים על שישי/שבת (weekday() 4=שישי, 5=שבת) - בהם יש לרוב
                # שירות מצומצם/שונה משמעותית מיום חול רגיל
                def prev_weekday(d):
                    while d.weekday() in (4, 5):
                        d -= timedelta(days=1)
                    return d

                sample_date = prev_weekday(datetime.now().date() - timedelta(days=1))
                times, gtfs_route_id = list_departure_times(operator_id, line_number, line_ref, route_direction, sample_date)
                tried_dates = [sample_date]
                d_back = 2
                while not times and d_back <= 14:
                    sample_date = prev_weekday(datetime.now().date() - timedelta(days=d_back))
                    times, gtfs_route_id = list_departure_times(operator_id, line_number, line_ref, route_direction, sample_date)
                    tried_dates.append(sample_date)
                    d_back += 1
                st.session_state["available_times"] = times
                if gtfs_route_id:
                    st.caption(f"gtfs_route_id: `{gtfs_route_id}` · נבדק על תאריך {sample_date} (יום {['שני','שלישי','רביעי','חמישי','שישי','שבת','ראשון'][sample_date.weekday()]})")
                if not times:
                    st.warning(f"לא נמצאו שעות יציאה ב-{len(tried_dates)} ימי החול האחרונים שנבדקו.")
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

max_workers = st.slider("מקביליות (מס' בקשות במקביל)", 1, 8, 4)
st.caption("שים לב: מקביליות גבוהה מדי עלולה לגרום ל-500 מהשרת של Hasadna - מומלץ להתחיל נמוך.")

st.divider()

if st.button("▶️ התחל איסוף", type="primary", disabled=not (operator_id and line_number and departure_times and line_ref)):
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
            executor.submit(collect_one, operator_id, line_number, line_ref, route_direction, d, t): (d, t)
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

    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=["line", "date", "scheduled_time", "status", "siri_ride_id", "gps_count", "gps_samples"])
    writer.writeheader()
    writer.writerows(rows)
    st.download_button("⬇️ הורד כקובץ CSV", data=csv_buf.getvalue().encode("utf-8"), file_name="bus_gps_data.csv", mime="text/csv")
