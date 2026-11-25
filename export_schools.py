#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学区对口数据导出（全市，公办小学 + 初中）
================================
用法：

    python3 export_schools.py                 # 默认读 ../shanghai-ai-housing/data/housing.db
    python3 export_schools.py --db /path/to/housing.db

从主仓库 shanghai-ai-housing 的数据库读「小区 → 对口学校」预计算结果
（estate_schools 表，由主仓库 `scripts/estates.py schools` 根据各区教育局
招生地段公示算出），按街镇聚合成 data/school_districts.json，供 build_map.py
内联进地图。数据库只读打开，不会改动主仓库。

小区落到哪个街镇：
  1. estates.street 与 towns.json 的街镇全名（同区）完全一致 → 直接用；
  2. 否则用小区坐标（estate_geo，高德 GCJ-02，先转 WGS-84）做点在多边形判断；
  3. 都不行的小区不进地图（数量在结尾打印）。

输出结构：
  meta  : 来源、年份、街镇数、学校数、小区数等
  towns : { 街镇全名: { "primary": {n, schools:[{name, areas:[{n, note?}]}]},
                        "junior":  {...} } }
  areas 里每条是一个小区；note 标注需要留意的情况：
    部分     对口表该条目标了「（部分）」，小区只有部分楼栋对口
    门牌段   按门牌号段命中，小区可能只有部分门牌在段内
    补充     来自补充公示
    经XX小学 初中由对口小学推出（对口表按小学划初中）
    一贯制直升（推断） 不是正式对口，是一贯制学校的直升推断
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from collections import OrderedDict, defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.normpath(os.path.join(HERE, "..", "shanghai-ai-housing", "data", "housing.db"))
OUT = os.path.join(HERE, "data", "school_districts.json")
STAGES = ("primary", "junior")


# ── GCJ-02 → WGS-84（底图和 towns.json 都是 WGS-84）──
_A, _EE = 6378245.0, 0.00669342162296594323


def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tlng(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def gcj2wgs(lng, lat):
    dlat, dlng = _tlat(lng - 105.0, lat - 35.0), _tlng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sq) * math.pi)
    dlng = (dlng * 180.0) / (_A / sq * math.cos(rad) * math.pi)
    return lng - dlng, lat - dlat


def point_in_ring(lng, lat, ring):
    """射线法。ring 顶点格式为 [lat, lng]（与 build_map.py 一致）。"""
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][1], ring[i][0]
        x2, y2 = ring[i + 1][1], ring[i + 1][0]
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def note_of(r):
    parts = []
    if r["is_inferred"]:
        parts.append("一贯制直升（推断）")
    if r["is_partial"]:
        parts.append("部分")
    if r["is_range"]:
        parts.append("门牌段")
    if r["is_supplement"]:
        parts.append("补充")
    if r["via_primary"]:
        parts.append(f"经{r['via_primary']}")
    return " / ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", default=DEFAULT_DB, help=f"主仓库数据库（默认 {DEFAULT_DB}）")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        sys.exit(f"找不到主仓库数据库：{args.db}")

    with open(os.path.join(HERE, "data", "towns.json"), encoding="utf-8") as f:
        towns_geo = json.load(f)
    town_by_name = {(t["dist"], t["name"]): t for t in towns_geo}

    def locate(lng, lat):
        for t in towns_geo:
            if any(point_in_ring(lng, lat, r) for r in t["rings"]):
                return t["name"]
        return None

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # 小区坐标：优先主来源那条链接的坐标
    geo = {}
    for r in conn.execute(
            "SELECT l.estate_id, g.lng, g.lat, l.match = 'primary' AS pri FROM estate_links l "
            "JOIN estate_geo g ON g.source = l.source AND g.source_key = l.source_key "
            "WHERE g.status = 'ok' AND g.lng IS NOT NULL ORDER BY pri DESC, l.source, l.source_key"):
        geo.setdefault(r["estate_id"], (r["lng"], r["lat"]))

    rows = conn.execute(
        "SELECT es.*, e.name AS estate, e.district AS e_dist, e.street FROM estate_schools es "
        "JOIN estates e ON e.id = es.estate_id "
        f"WHERE es.stage IN ({','.join('?' * len(STAGES))}) "
        "ORDER BY es.estate_id, es.stage, es.school", STAGES).fetchall()
    years = sorted({r["zone_year"] for r in rows})

    # 小区 → 街镇
    estate_town, how = {}, defaultdict(int)
    for r in rows:
        eid = r["estate_id"]
        if eid in estate_town:
            continue
        t = None
        st = (r["street"] or "").strip()
        if st and (r["e_dist"], st) in town_by_name:
            t, k = st, "street"
        elif eid in geo:
            t, k = locate(*gcj2wgs(*geo[eid])), "coord"
        else:
            k = "none"
        estate_town[eid] = t
        how[k if t else "miss"] += 1

    # town → stage → school → estate → note
    agg = defaultdict(lambda: defaultdict(lambda: defaultdict(OrderedDict)))
    for r in rows:
        t = estate_town[r["estate_id"]]
        if not t:
            continue
        areas = agg[t][r["stage"]][r["school"]]
        note = note_of(r)
        prev = areas.get(r["estate"])
        if prev is None:
            areas[r["estate"]] = note
        elif note and note not in prev:
            areas[r["estate"]] = " / ".join(x for x in (prev, note) if x)

    out_towns = OrderedDict()
    schools_seen = {s: set() for s in STAGES}
    n_area = 0
    town_dist = {t["name"]: t["dist"] for t in towns_geo}
    for t in sorted(agg, key=lambda k: (town_dist.get(k, ""), k)):
        entry = OrderedDict()
        for stage in STAGES:
            sch = agg[t].get(stage)
            if not sch:
                continue
            lst = []
            for sname, areas in sorted(sch.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                lst.append({"name": sname, "areas": [
                    {"n": n, **({"note": note} if note else {})} for n, note in sorted(areas.items())]})
                schools_seen[stage].add(sname)
                n_area += len(areas)
            entry[stage] = {"n": sum(len(s["areas"]) for s in lst), "schools": lst}
        out_towns[t] = entry

    dists = sorted({town_dist[t] for t in out_towns})
    # 各区收录情况：有对口记录并落进街镇的小区 / 主库该区小区总数（各区差异很大，页面上要说清楚）
    total_by_dist = dict(conn.execute(
        "SELECT district, COUNT(*) FROM estates WHERE district IS NOT NULL GROUP BY district").fetchall())
    got_by_dist = defaultdict(int)
    for t in estate_town.values():
        if t:
            got_by_dist[town_dist[t]] += 1
    coverage = {d: [got_by_dist[d], total_by_dist.get(d, 0)] for d in sorted(total_by_dist)}
    meta = {
        "title": f"上海市 {'/'.join(map(str, years))} 年公办小学、初中招生地段（按小区）",
        "year": years[-1] if years else None,
        "stages": list(STAGES),
        "districts": len(dists),
        "source": f"上海市各区教育局 {'/'.join(map(str, years))} 年义务教育阶段公办学校招生地段公示",
        "towns": len(out_towns),
        "primary_schools": len(schools_seen["primary"]),
        "junior_schools": len(schools_seen["junior"]),
        "coverage": coverage,
        "estates": sum(1 for v in estate_town.values() if v),
        "areas": n_area,
        "built": date.today().isoformat(),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "towns": out_towns}, f, ensure_ascii=False, separators=(",", ":"))

    total = len(estate_town)
    print(f"小区 {total} 个：街镇名直接匹配 {how['street']}，坐标落点 {how['coord']}，"
          f"落不进街镇 {how['miss']}（未进地图）")
    print(f"→ {meta['districts']} 个区 / {meta['towns']} 个街镇；小学 {meta['primary_schools']} 所、"
          f"初中 {meta['junior_schools']} 所；"
          f"{n_area} 条学校×小区")
    print("各区收录小区：" + "，".join(f"{d} {a}/{b}" for d, (a, b) in coverage.items()))
    print(f"已生成：{OUT}（{os.path.getsize(OUT) / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
