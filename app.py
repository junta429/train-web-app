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

# 午後以降は「翌朝の時刻表」として扱う
NEXT_DAY_SWITCH_HOUR = 12

# 都庁前到着後もしばらく入力できるように残す時間
POST_ARRIVAL_DISPLAY_MINUTES = 90

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
# DB初期化 / 旧カラム移行
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS route_status (
        tx_dep TEXT NOT NULL,
        oedo_dep TEXT NOT NULL,
        tx_crowded TEXT NOT NULL DEFAULT '',
        oedo7_crowded TEXT NOT NULL DEFAULT '',
        oedo8_crowded TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tx_dep, oedo_dep)
    )
    """)

    # 旧カラムがある場合の移行対応
    c.execute("PRAGMA table_info(route_status)")
    cols = [row[1] for row in c.fetchall()]

    if "tx_crowded" not in cols:
        c.execute("ALTER TABLE route_status ADD COLUMN tx_crowded TEXT NOT NULL DEFAULT ''")

    if "oedo7_crowded" not in cols:
        c.execute("ALTER TABLE route_status ADD COLUMN oedo7_crowded TEXT NOT NULL DEFAULT ''")

    if "oedo8_crowded" not in cols:
        c.execute("ALTER TABLE route_status ADD COLUMN oedo8_crowded TEXT NOT NULL DEFAULT ''")

    if "updated_at" not in cols:
        c.execute("ALTER TABLE route_status ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")

    # 旧6号車列の値を7号車へ引き継ぐ
    if "oedo6_crowded" in cols:
        c.execute("""
        UPDATE route_status
        SET oedo7_crowded = CASE
            WHEN oedo7_crowded = '' THEN oedo6_crowded
            ELSE oedo7_crowded
        END
        """)

    conn.commit()
    conn.close()


# =========================
# 現在時刻
# =========================
def now_jst() -> datetime:
    return datetime.now(JST)


# =========================
# サービス日判定
# 午後以降は翌朝の便を見たい想定
# =========================
def get_service_base_dt(now_dt: datetime) -> datetime:
    if now_dt.hour >= NEXT_DAY_SWITCH_HOUR:
        base = now_dt + timedelta(days=1)
    else:
        base = now_dt
    return base.replace(hour=0, minute=0, second=0, microsecond=0)


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

    c.execute("PRAGMA table_info(route_status)")
    cols = [row[1] for row in c.fetchall()]

    if "oedo7_crowded" in cols:
        c.execute("""
        SELECT tx_dep, oedo_dep, tx_crowded, oedo7_crowded, oedo8_crowded
        FROM route_status
        """)
        rows = c.fetchall()
        conn.close()

        status_map = {}
        for tx_dep, oedo_dep, tx_crowded, oedo7_crowded, oedo8_crowded in rows:
            status_map[(tx_dep, oedo_dep)] = {
                "tx_crowded": tx_crowded,
                "oedo7_crowded": oedo7_crowded,
                "oedo8_crowded": oedo8_crowded,
            }
        return status_map

    # 万一旧構造だけでも落ちないようにする
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
            "oedo7_crowded": oedo6_crowded,
            "oedo8_crowded": oedo8_crowded,
        }
    return status_map


# =========================
# 状態保存
# =========================
def save_status(tx_dep, oedo_dep, tx_crowded, oedo7_crowded, oedo8_crowded):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    INSERT INTO route_status (
        tx_dep, oedo_dep, tx_crowded, oedo7_crowded, oedo8_crowded, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(tx_dep, oedo_dep)
    DO UPDATE SET
        tx_crowded = excluded.tx_crowded,
        oedo7_crowded = excluded.oedo7_crowded,
        oedo8_crowded = excluded.oedo8_crowded,
        updated_at = excluded.updated_at
    """, (
        tx_dep,
        oedo_dep,
        tx_crowded,
        oedo7_crowded,
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
    base_dt = get_service_base_dt(now_dt)

    groups = []

    for tx in tx_list:
        tx_dep = tx["八潮発"]
        tx_arr = tx["新御徒町着"]
        tx_type = tx.get("TX種別", "")
        tx_first = tx.get("TX始発", "")

        if tx_arr is None:
            continue

        tx_dep_dt = parse_time(tx_dep, base_dt)
        tx_arr_dt = parse_time(tx_arr, base_dt)

        if tx_arr_dt < tx_dep_dt:
            tx_arr_dt += timedelta(days=1)

        candidates = []

        for oe in oedo_list:
            oe_dep = oe["大江戸線新御徒町発"]
            oe_arr = oe["都庁前着"]

            if oe_arr is None:
                continue

            oe_dep_dt = parse_time(oe_dep, base_dt)
            oe_arr_dt = parse_time(oe_arr, base_dt)

            if oe_arr_dt < oe_dep_dt:
                oe_arr_dt += timedelta(days=1)

            transfer = int((oe_dep_dt - tx_arr_dt).total_seconds() // 60)

            if MIN_TRANSFER <= transfer <= MAX_WAIT:
                total = int((oe_arr_dt - tx_dep_dt).total_seconds() // 60)

                # 到着後もしばらく表示を残す
                visible_until = oe_arr_dt + timedelta(minutes=POST_ARRIVAL_DISPLAY_MINUTES)
                if visible_until < now_dt:
                    continue

                status = status_map.get((tx_dep, oe_dep), {
                    "tx_crowded": "",
                    "oedo7_crowded": "",
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
                    "大江戸線7号車混雑": status["oedo7_crowded"],
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
            "tx_arr": tx_arr,
            "tx_type": tx_type,
            "tx_first": tx_first,
            "minutes_left": int((tx_dep_dt - now_dt).total_seconds() // 60),
            "routes": final,
            "sort_dt": tx_arr_dt,
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
        request.form["oedo7_crowded"],
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