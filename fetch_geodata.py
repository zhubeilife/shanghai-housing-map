#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海买房板块地图 · 地理数据抓取脚本（可选，一般不需要运行）
==========================================================
data/ 目录已带好一份现成数据。只有在以下情况才需要重跑：
  · 地铁开通新线路 / 延伸段
  · 行政区划或街镇边界调整
  · data/ 目录损坏或丢失

用法（仅需 Python 3 标准库，需联网）：

    python3 fetch_geodata.py            # 全部重新抓取
    python3 fetch_geodata.py metro      # 只抓地铁
    python3 fetch_geodata.py rings      # 只抓环线
    python3 fetch_geodata.py towns      # 只抓街镇边界（最慢，约1-3分钟）
    python3 fetch_geodata.py districts  # 只抓区界

数据来源：
  · 区界：阿里云 DataV（geo.datav.aliyun.com）
  · 街镇边界 / 环线 / 地铁：OpenStreetMap（Overpass API）
Overpass 公共服务器偶尔繁忙，失败时脚本会自动换镜像重试；
如仍失败，隔几分钟再跑一次即可。
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
sys.setrecursionlimit(50000)


# ──────────────────────────── 通用工具 ────────────────────────────

def overpass(query, tries_per_mirror=2):
    """向 Overpass 提交查询，自动轮换镜像。返回解析后的 JSON。"""
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for mirror in OVERPASS_MIRRORS:
        for _ in range(tries_per_mirror):
            try:
                req = urllib.request.Request(mirror, data=body,
                                             headers={"User-Agent": "sh-housing-map/1.0"})
                with urllib.request.urlopen(req, timeout=320) as r:
                    return json.loads(r.read().decode())
            except Exception as e:  # 服务器忙/超时 → 换镜像
                last = e
                print(f"  {mirror} 失败（{e}），重试…")
                time.sleep(3)
    raise RuntimeError(f"所有 Overpass 镜像均失败：{last}")


def dp(pts, tol):
    """Douglas-Peucker 抽稀。"""
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dmax, idx = 0, 0
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy) or 1e-12
    for i in range(1, len(pts) - 1):
        x0, y0 = pts[i]
        d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm
        if d > dmax:
            dmax, idx = d, i
    if dmax > tol:
        return dp(pts[:idx + 1], tol)[:-1] + dp(pts[idx:], tol)
    return [pts[0], pts[-1]]


def simplify(pts, tol):
    if len(pts) < 3:
        return pts
    if pts[0] == pts[-1]:  # 闭合环从中间劈开，避免被抽成两个点
        h = len(pts) // 2
        out = dp(pts[:h + 1], tol)[:-1] + dp(pts[h:], tol)
        if out[0] != out[-1]:
            out.append(out[0])
        return out
    return dp(pts, tol)


def stitch(segs, snap=4):
    """把散乱的 way 线段按端点吻合拼接成长折线。"""
    segs = [s[:] for s in segs if len(s) > 1]
    out = []
    key = lambda p: (round(p[0], snap), round(p[1], snap))
    while segs:
        cur = segs.pop(0)
        changed = True
        while changed:
            changed = False
            for i, s in enumerate(segs):
                if key(s[0]) == key(cur[-1]):
                    cur = cur + s[1:]; segs.pop(i); changed = True; break
                if key(s[-1]) == key(cur[-1]):
                    cur = cur + s[-2::-1]; segs.pop(i); changed = True; break
                if key(s[-1]) == key(cur[0]):
                    cur = s[:-1] + cur; segs.pop(i); changed = True; break
                if key(s[0]) == key(cur[0]):
                    cur = s[::-1][:-1] + cur; segs.pop(i); changed = True; break
        out.append(cur)
    return out


def way_pts(m):
    return [(round(p["lon"], 5), round(p["lat"], 5)) for p in m["geometry"]]


def to_latlng(seg):
    return [[p[1], p[0]] for p in seg]


# ──────────────────────────── 各数据集 ────────────────────────────

def fetch_districts():
    """16 个区的边界（阿里云 DataV，GeoJSON）。"""
    print("▸ 抓取区界（DataV）…")
    url = "https://geo.datav.aliyun.com/areas_v3/bound/310000_full.json"
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.loads(r.read().decode())
    for f in d["features"]:
        p = f["properties"]
        f["properties"] = {"name": p["name"], "center": p.get("center") or p.get("centroid")}
    json.dump(d, open(os.path.join(DATA, "districts.json"), "w"), ensure_ascii=False)
    print(f"  完成，{len(d['features'])} 个区")


def fetch_metro():
    """地铁线路：每条线取一个方向的 relation，拼接后抽稀。"""
    print("▸ 抓取地铁线路（Overpass）…")
    tags = overpass('[out:json][timeout:180];'
                    'area["name"="上海市"]["admin_level"="4"]->.sh;'
                    'relation["route"="subway"](area.sh);out tags;')
    best = {}
    for r in tags["elements"]:
        t = r.get("tags", {})
        ref = t.get("ref")
        if ref and ref not in best:  # 每条线保留遇到的第一个方向
            best[ref] = (r["id"], t.get("colour", "#888"))
    print(f"  找到 {len(best)} 条线：{sorted(best, key=lambda x: (len(x), x))}")

    ids = ",".join(str(v[0]) for v in best.values())
    geom = overpass(f"[out:json][timeout:300];relation(id:{ids});out geom;")
    id2ref = {v[0]: k for k, v in best.items()}
    lines = {}
    for rel in geom["elements"]:
        ref = id2ref[rel["id"]]
        segs = [way_pts(m) for m in rel.get("members", [])
                if m["type"] == "way" and m.get("geometry")
                and m.get("role", "") in ("", "forward", "backward")]
        paths = [simplify(s, 0.0004) for s in stitch(segs)]
        lines[ref] = {"colour": best[ref][1], "paths": [to_latlng(s) for s in paths]}
    json.dump(lines, open(os.path.join(DATA, "metro_lines.json"), "w"), ensure_ascii=False)
    print("  完成 → data/metro_lines.json")


def fetch_rings():
    """内环 / 中环 / 外环 S20 / 郊环 G1503。"""
    print("▸ 抓取环线公路（Overpass）…")
    d = overpass('[out:json][timeout:300];'
                 'area["name"="上海市"]["admin_level"="4"]->.sh;'
                 '('
                 ' way["name"="内环高架路"](area.sh);'
                 ' relation["name"="中环路"]["type"="route"](area.sh);'
                 ' relation["ref"="S20"]["route"="road"](area.sh);'
                 ' relation["ref"="G1503"]["route"="road"](area.sh);'
                 ');out geom;')
    inner, middle, outer, suburb = [], [], [], []
    for e in d["elements"]:
        if e["type"] == "way" and e.get("tags", {}).get("name") == "内环高架路":
            inner.append(way_pts(e))
        elif e["type"] == "relation":
            t = e.get("tags", {})
            if t.get("name") == "外环高速（内圈）":
                continue  # 双向只留一圈
            tgt = (middle if t.get("name") == "中环路"
                   else outer if t.get("ref") == "S20" else suburb)
            for m in e.get("members", []):
                if m["type"] == "way" and m.get("geometry") and m.get("role") != "link":
                    tgt.append(way_pts(m))
    result = {}
    for name, segs, min_pts in [("inner", inner, 10), ("middle", middle, 8),
                                ("outer", outer, 10), ("suburb", suburb, 10)]:
        st = sorted(stitch(segs), key=len, reverse=True)
        keep = [s for s in st if len(s) >= min_pts] or st[:1]
        result[name] = [to_latlng(simplify(s, 0.0005)) for s in keep]
        print(f"  {name}: {len(keep)} 段")
    json.dump(result, open(os.path.join(DATA, "ring_roads.json"), "w"), ensure_ascii=False)
    print("  完成 → data/ring_roads.json")


def fetch_towns():
    """全市街镇级（admin_level=8）边界，含所属区判定与面积。"""
    print("▸ 抓取街镇边界（Overpass，最慢一步）…")
    tags = overpass('[out:json][timeout:180];'
                    'area["name"="上海市"]["admin_level"="4"]->.sh;'
                    'relation["boundary"="administrative"]["admin_level"="8"](area.sh);'
                    'out tags;')
    rels = [(e["id"], e["tags"].get("name", "")) for e in tags["elements"]
            if e["type"] == "relation"]
    print(f"  共 {len(rels)} 个街镇，分批抓取几何…")

    towns = []
    for i in range(0, len(rels), 40):
        batch = rels[i:i + 40]
        ids = ",".join(str(r[0]) for r in batch)
        geom = overpass(f"[out:json][timeout:300];relation(id:{ids});out geom;")
        name_of = dict(batch)
        for rel in geom["elements"]:
            if rel["type"] != "relation":
                continue
            segs = [way_pts(m) for m in rel.get("members", [])
                    if m["type"] == "way" and m.get("role") == "outer" and m.get("geometry")]
            # 拼环：外边界 way 首尾相接成闭合环
            rings, pool = [], [s[:] for s in segs if len(s) > 1]
            key = lambda p: (round(p[0], 5), round(p[1], 5))
            while pool:
                cur = pool.pop(0)
                while key(cur[0]) != key(cur[-1]):
                    for j, s in enumerate(pool):
                        if key(s[0]) == key(cur[-1]):
                            cur += s[1:]; pool.pop(j); break
                        if key(s[-1]) == key(cur[-1]):
                            cur += s[-2::-1]; pool.pop(j); break
                    else:
                        break
                if key(cur[0]) == key(cur[-1]) and len(cur) > 3:
                    rings.append(cur)

            def area(r):
                a = 0
                for j in range(len(r) - 1):
                    a += r[j][0] * r[j + 1][1] - r[j + 1][0] * r[j][1]
                return abs(a) / 2

            rings = [simplify(r, 0.0006) for r in rings if area(r) > 1e-6]
            if not rings:
                continue
            big = max(rings, key=area)
            cx = sum(p[0] for p in big[:-1]) / (len(big) - 1)
            cy = sum(p[1] for p in big[:-1]) / (len(big) - 1)
            towns.append({"name": name_of[rel["id"]],
                          "c": [round(cy, 4), round(cx, 4)],
                          "a": round(sum(area(r) for r in rings), 6),
                          "rings": [to_latlng(r) for r in rings]})
        print(f"  {min(i + 40, len(rels))}/{len(rels)}")
        time.sleep(2)

    # 所属区判定（街镇质心落在哪个区界内）
    dist = json.load(open(os.path.join(DATA, "districts.json"), encoding="utf-8"))

    def pip(x, y, ring):
        inside = False
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]; x2, y2 = ring[i + 1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside

    kept = []
    for t in towns:
        y, x = t["c"]
        for f in dist["features"]:
            g = f["geometry"]
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            if any(pip(x, y, poly[0]) for poly in polys):
                t["dist"] = (f["properties"]["name"]
                             .replace("新区", "").replace("区", ""))
                kept.append(t)
                break
        # 质心不在任何区内（江苏交界处偶有误入的关系）→ 丢弃
    json.dump(kept, open(os.path.join(DATA, "towns.json"), "w"), ensure_ascii=False)
    print(f"  完成 → data/towns.json（{len(kept)} 个街镇）")


if __name__ == "__main__":
    want = sys.argv[1] if len(sys.argv) > 1 else "all"
    if want in ("all", "districts"):
        fetch_districts()
    if want in ("all", "metro"):
        fetch_metro()
    if want in ("all", "rings"):
        fetch_rings()
    if want in ("all", "towns"):
        fetch_towns()
    print("完成。接下来运行 python3 build_map.py 重新生成地图。")
