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

# 都庁前到着後もしばらく入力できるように残す時間
POST_ARRIVAL_DISPLAY_MINUTES = 90

# 一番上の基準にしたい発車時刻
TOP_ANCHOR_TIME = "05:03"

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
# 時刻処理
# =========================
def parse_time(text: str, base_dt: datetime) -> datetime:
    h, m = map(int, text.split(":"))
    return base_dt.replace(hour=h, minute=m, second=0, microsecond=0)


def time_to_minutes(text: str) -> int:
    h, m = map(int, text.split(":"))
    return h * 60 + m


def anchored_sort_key(text: str, anchor_text: str = TOP_ANCHOR_TIME) -> int:
    """
    05:03を先頭基準にして1日を並べるためのキー
    例:
    05:03 -> 0
    05:10 -> 7
    23:00 -> ...
    00:10 -> 翌日側として後ろ
    """
    minutes = time_to_minutes(text)
    anchor = time_to_minutes(anchor_text)
    return (minutes - anchor) % (24 * 60)


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

    # 旧構造でも落ちないようにする
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
def get_routes(now_dt: datetime):
    tx_list, oedo_list = load_data()
    status_map = load_status_map()

    groups = []

    today_base = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_base = today_base + timedelta(days=1)

    for tx in tx_list:
        tx_dep = tx["八潮発"]
        tx_arr = tx["新御徒町着"]
        tx_type = tx.get("TX種別", "")
        tx_first = tx.get("TX始発", "")

        if tx_arr is None:
            continue

        candidate_groups = []

        # 今日の便 / 翌日の便候補を見る
        for service_base in [today_base, tomorrow_base]:
            tx_dep_dt = parse_time(tx_dep, service_base)
            tx_arr_dt = parse_time(tx_arr, service_base)

            if tx_arr_dt < tx_dep_dt:
                tx_arr_dt += timedelta(days=1)

            candidates = []

            for oe in oedo_list:
                oe_dep = oe["大江戸線新御徒町発"]
                oe_arr = oe["都庁前着"]

                if oe_arr is None:
                    continue

                oe_dep_dt = parse_time(oe_dep, service_base)
                oe_arr_dt = parse_time(oe_arr, service_base)

                if oe_arr_dt < oe_dep_dt:
                    oe_arr_dt += timedelta(days=1)

                transfer = int((oe_dep_dt - tx_arr_dt).total_seconds() // 60)

                if MIN_TRANSFER <= transfer <= MAX_WAIT:
                    total = int((oe_arr_dt - tx_dep_dt).total_seconds() // 60)

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
                        "oedo_arr_dt": oe_arr_dt,
                        "form_id": f"form-{tx_dep.replace(':', '')}-{oe_dep.replace(':', '')}-{service_base.strftime('%Y%m%d')}",
                    })

            if not candidates:
                continue

            short = [c for c in candidates if c["乗換"] <= 2]
            long = [c for c in candidates if c["乗換"] >= 3]

            final = short.copy()
            if long:
                final.append(min(long, key=lambda x: x["oedo_dep_dt"]))

            final.sort(key=lambda x: x["oedo_dep_dt"])

            candidate_groups.append({
                "tx_dep": tx_dep,
                "tx_arr": tx_arr,
                "tx_type": tx_type,
                "tx_first": tx_first,
                "tx_dep_dt": tx_dep_dt,
                "tx_arr_dt": tx_arr_dt,
                "routes": final,
            })

        # 今日と明日の両方候補があれば、より近い方を採用
        if not candidate_groups:
            continue

        candidate_groups.sort(key=lambda g: abs((g["tx_dep_dt"] - now_dt).total_seconds()))
        selected = candidate_groups[0]

        first_tocho_arr = selected["routes"][0]["都庁前着"]
        first_tocho_arr_dt = selected["routes"][0]["oedo_arr_dt"]

        groups.append({
            "tx_dep": selected["tx_dep"],
            "tx_arr": selected["tx_arr"],
            "first_tocho_arr": first_tocho_arr,
            "tx_type": selected["tx_type"],
            "tx_first": selected["tx_first"],
            "minutes_left": int((selected["tx_dep_dt"] - now_dt).total_seconds() // 60),
            "routes": selected["routes"],
            "sort_dt": selected["tx_arr_dt"],
            "focus_dt": selected["tx_dep_dt"],
            "group_id": f"group-{selected['tx_dep'].replace(':', '')}-{selected['tx_dep_dt'].strftime('%Y%m%d')}",
            "first_tocho_arr_dt": first_tocho_arr_dt,
        })

    # 05:03発を常に先頭にした並びへ変更
    groups.sort(key=lambda x: anchored_sort_key(x["tx_dep"], TOP_ANCHOR_TIME))

    # まだ出発していない一番近い列車を探す（枠線表示用だけ）
    focused = False
    for g in groups:
        g["focus_group"] = False
        if not focused and g["focus_dt"] >= now_dt:
            g["focus_group"] = True
            focused = True

    # もう全部出発済みなら、並び順の先頭を対象に
    if groups and not focused:
        groups[0]["focus_group"] = True

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