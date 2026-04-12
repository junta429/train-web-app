from flask import Flask, render_template, request, redirect
import json
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
JSON_FILE = os.path.join(BASE_DIR, "train_data.json")
DB_FILE = os.path.join(BASE_DIR, "status.db")

MIN_TRANSFER = 1
MAX_WAIT = 10
JST = ZoneInfo("Asia/Tokyo")

CROWD_OPTIONS = [
    "余裕で座れる",
    "ギリギリ座れる",
    "ゆったり立てる",
    "隅に立てる",
    "肩当たる",
    "おしくらまんじゅう",
]

# 濃い→薄い→薄い→濃い→薄い→濃い
CROWD_STYLE_MAP = {
    "余裕で座れる": "crowd-blue-strong",
    "ギリギリ座れる": "crowd-blue-light",
    "ゆったり立てる": "crowd-yellow-light",
    "隅に立てる": "crowd-yellow-strong",
    "肩当たる": "crowd-orange-light",
    "おしくらまんじゅう": "crowd-orange-strong",
}


# =========================
# DB初期化
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS route_status (
        tx_dep TEXT NOT NULL,
        oedo_dep TEXT NOT NULL,
        tx_crowded TEXT NOT NULL DEFAULT '',
        oedo6_crowded TEXT NOT NULL DEFAULT '',
        oedo8_crowded TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tx_dep, oedo_dep)
    )
    """)

    conn.commit()
    conn.close()


# =========================
# 現在時刻
# =========================
def now_jst() -> datetime:
    return datetime.now(JST)


# =========================
# 時刻処理
# =========================
def parse_time(text: str, base_dt: datetime) -> datetime:
    h, m = map(int, text.split(":"))
    return base_dt.replace(hour=h, minute=m, second=0, microsecond=0)


# =========================
# JSON読み込み
# =========================
def load_data():
    with open(JSON_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data["tx_results"], data["oedo_results"]


# =========================
# 状態読み込み
# =========================
def load_status_map():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    SELECT tx_dep, oedo_dep, tx_crowded, oedo6_crowded, oedo8_crowded
    FROM route_status
    """)
    rows = c.fetchall()

    conn.close()

    status_map = {}
    for tx_dep, oedo_dep, tx_crowded, oedo6_crowded, oedo8_crowded in rows:
        status_map[(tx_dep, oedo_dep)] = {
            "tx_crowded": tx_crowded,
            "oedo6_crowded": oedo6_crowded,
            "oedo8_crowded": oedo8_crowded,
        }

    return status_map


# =========================
# 状態保存
# =========================
def save_status(tx_dep, oedo_dep, tx_crowded, oedo6_crowded, oedo8_crowded):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    INSERT INTO route_status (
        tx_dep, oedo_dep, tx_crowded, oedo6_crowded, oedo8_crowded, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(tx_dep, oedo_dep)
    DO UPDATE SET
        tx_crowded = excluded.tx_crowded,
        oedo6_crowded = excluded.oedo6_crowded,
        oedo8_crowded = excluded.oedo8_crowded,
        updated_at = excluded.updated_at
    """, (
        tx_dep,
        oedo_dep,
        tx_crowded,
        oedo6_crowded,
        oedo8_crowded,
        now_jst().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()


# =========================
# ルート取得
# =========================
def get_routes(now_dt):
    tx_list, oedo_list = load_data()
    status_map = load_status_map()

    groups = []

    for tx in tx_list:
        tx_dep = tx["八潮発"]
        tx_arr = tx["新御徒町着"]
        tx_type = tx.get("TX種別", "")
        tx_first = tx.get("TX始発", "")

        if tx_arr is None:
            continue

        tx_dep_dt = parse_time(tx_dep, now_dt)
        tx_arr_dt = parse_time(tx_arr, now_dt)

        if tx_dep_dt < now_dt:
            tx_dep_dt += timedelta(days=1)
            tx_arr_dt += timedelta(days=1)

        candidates = []

        for oe in oedo_list:
            oe_dep = oe["大江戸線新御徒町発"]
            oe_arr = oe["都庁前着"]

            if oe_arr is None:
                continue

            oe_dep_dt = parse_time(oe_dep, now_dt)
            oe_arr_dt = parse_time(oe_arr, now_dt)

            if oe_dep_dt < now_dt:
                oe_dep_dt += timedelta(days=1)
                oe_arr_dt += timedelta(days=1)

            transfer = int((oe_dep_dt - tx_arr_dt).total_seconds() // 60)

            if MIN_TRANSFER <= transfer <= MAX_WAIT:
                total = int((oe_arr_dt - tx_dep_dt).total_seconds() // 60)

                status = status_map.get((tx_dep, oe_dep), {
                    "tx_crowded": "",
                    "oedo6_crowded": "",
                    "oedo8_crowded": "",
                })

                candidates.append({
                    "tx": tx_dep,
                    "tx_arr": tx_arr,
                    "大江戸線発": oe_dep,
                    "都庁前着": oe_arr,
                    "乗換": transfer,
                    "総時間": total,
                    "TX混雑": status["tx_crowded"],
                    "大江戸線6号車混雑": status["oedo6_crowded"],
                    "大江戸線8号車混雑": status["oedo8_crowded"],
                    "oedo_dep_dt": oe_dep_dt,
                    "form_id": f"form-{tx_dep.replace(':', '')}-{oe_dep.replace(':', '')}",
                })

        if not candidates:
            continue

        short = [c for c in candidates if c["乗換"] <= 2]
        long = [c for c in candidates if c["乗換"] >= 3]

        final = short.copy()
        if long:
            final.append(min(long, key=lambda x: x["oedo_dep_dt"]))

        final.sort(key=lambda x: x["oedo_dep_dt"])

        groups.append({
            "tx_dep": tx_dep,
            "tx_type": tx_type,
            "tx_first": tx_first,
            "minutes_left": int((tx_dep_dt - now_dt).total_seconds() // 60),
            "routes": final,
            "sort_dt": tx_dep_dt,
        })

    groups.sort(key=lambda x: x["sort_dt"])
    return groups


# =========================
# 保存
# =========================
@app.route("/save", methods=["POST"])
def save():
    save_status(
        request.form["tx_dep"],
        request.form["oedo_dep"],
        request.form["tx_crowded"],
        request.form["oedo6_crowded"],
        request.form["oedo8_crowded"],
    )
    return redirect("/")


# =========================
# 表示
# =========================
@app.route("/")
def index():
    now_dt = now_jst()
    groups = get_routes(now_dt)

    return render_template(
        "index.html",
        now=now_dt.strftime("%H:%M"),
        groups=groups,
        crowd_options=CROWD_OPTIONS,
        crowd_style_map=CROWD_STYLE_MAP,
    )


# =========================
# 起動
# =========================
if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=8000, debug=True, use_reloader=False)