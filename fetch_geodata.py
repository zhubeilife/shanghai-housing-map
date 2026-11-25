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

    python3 fetch_geodata.py                # 全部重新抓取
    python3 fetch_geodata.py metro          # 只抓地铁（已开通线路）
    python3 fetch_geodata.py rings          # 只抓环线
    python3 fetch_geodata.py towns          # 只抓街镇边界（最慢，约1-3分钟）
    python3 fetch_geodata.py districts      # 只抓区界
    python3 fetch_geodata.py metro_planned  # 只抓在建/规划地铁（不纳入 all，数据不稳定需人工核对）

数据来源：
  · 区界：阿里云 DataV（geo.datav.aliyun.com）
  · 街镇边界 / 环线 / 地铁：OpenStreetMap（Overpass API）
Overpass 公共服务器偶尔繁忙，失败时脚本会自动换镜像重试；
如仍失败，隔几分钟再跑一次即可。
"""
import collections
import json
import math
import os
import re
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
    """地铁线路：同一 ref 的所有 relation（两个方向 + 支线，如 11 号线花桥支线、
    5 号线南延伸）合并轨道 way，按 way id 去重后拼接、去掉并行双线，再抽稀。"""
    print("▸ 抓取地铁线路（Overpass）…")
    # 用经纬度框而不是 area：部分镜像没建 area 索引，会静默返回空结果
    tags = overpass('[out:json][timeout:180];'
                    'relation["route"="subway"](30.65,120.85,31.90,122.25);out tags;')
    rels = collections.defaultdict(list)
    colour = {}
    for r in tags["elements"]:
        t = r.get("tags", {})
        ref = t.get("ref")
        if t.get("network") and "上海" not in t["network"]:
            continue  # 经纬度框会框进苏州轨道交通 11 号线（花桥换乘），编号撞车
        if ref and re.fullmatch(r"\d+", ref):
            rels[ref].append(r["id"])
            colour.setdefault(ref, t.get("colour", "#888"))
    if not rels:
        raise RuntimeError("Overpass 返回空结果，未覆盖 metro_lines.json")
    print(f"  找到 {len(rels)} 条线 / {sum(map(len, rels.values()))} 个 relation："
          f"{sorted(rels, key=int)}")

    id2ref = {i: ref for ref, ids in rels.items() for i in ids}
    ids = ",".join(str(i) for i in id2ref)
    geom = overpass(f"[out:json][timeout:300];relation(id:{ids});out geom;")
    # 每个 relation（一个方向或一条支线）单独拼接，再在同线各 relation 之间去掉重合的部分；
    # 不能先把两个方向的 way 混在一起拼：首尾相接会拼成"去程+回程"的一条折线，长度翻倍
    rel_paths = collections.defaultdict(list)  # ref → [折线]
    for rel in geom["elements"]:
        ref = id2ref[rel["id"]]
        segs = [way_pts(m) for m in rel.get("members", [])
                if m["type"] == "way" and m.get("geometry")
                and m.get("role", "") in ("", "forward", "backward")]
        rel_paths[ref] += [simplify(s, 0.0004) for s in stitch(segs)]
    lines = {}
    for ref in sorted(rel_paths, key=int):
        paths = _dedup_near([p for p in rel_paths[ref] if len(p) > 1])
        lines[ref] = {"colour": colour[ref], "paths": [to_latlng(s) for s in paths]}
        print(f"  {ref} 号线：{len(rels[ref])} 个 relation → {len(paths)} 段")
    json.dump(lines, open(os.path.join(DATA, "metro_lines.json"), "w"), ensure_ascii=False)
    print("  完成 → data/metro_lines.json")


def _dedup_near(paths, tol_m=80, min_len_m=300):
    """合并同一条线的多条折线（两个方向、支线）：从最长的开始，后面每条只保留没被已保留折线
    覆盖（tol_m 米内）的部分，且该部分长度要超过 min_len_m。这样双线/反向重复被去掉，
    支线（如 11 号线嘉定北支线、10 号线航中路支线）只留下分岔出去的那一段。"""
    kx, ky = 94800, 111000  # 上海纬度附近 1° 的米数

    def seg_dist(p, a, b):
        px, py = p[0] * kx, p[1] * ky
        ax, ay, bx, by = a[0] * kx, a[1] * ky, b[0] * kx, b[1] * ky
        dx, dy = bx - ax, by - ay
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / ((dx * dx + dy * dy) or 1)))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    def length(path):
        return sum(math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * ky) for a, b in zip(path, path[1:]))

    def densify(path, step_m=100):  # 抽稀后的点很稀，插点后再逐点判断覆盖
        out = [path[0]]
        for a, b in zip(path, path[1:]):
            n = max(1, int(math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * ky) // step_m))
            out += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n + 1)]
        return out

    kept = []
    for p in sorted(paths, key=length, reverse=True):
        dense = densify(p)
        covered = [any(seg_dist(pt, a, b) <= tol_m for k in kept for a, b in zip(k, k[1:])) for pt in dense]
        run = []
        for idx, pt in enumerate(dense + [None]):
            if pt is not None and not covered[idx]:
                if not run and idx > 0:
                    run.append(dense[idx - 1])  # 带上前一个点，和主线接上
                run.append(pt)
                continue
            if run:
                if pt is not None:
                    run.append(pt)
                if length(run) >= min_len_m:
                    kept.append(simplify(run, 0.0004))
                run = []
    return kept


def _bbox(seg):
    lats = [p[1] for p in seg]; lons = [p[0] for p in seg]
    return (min(lats), max(lats), min(lons), max(lons))


def _dedup_parallel(paths):
    """去掉双线并行轨道产生的近乎重复的线（保留更长的那条）。"""
    boxes = [_bbox(p) for p in paths]
    keep = [True] * len(paths)
    for i in range(len(paths)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(paths)):
            if not keep[j]:
                continue
            a1, a2, a3, a4 = boxes[i]; b1, b2, b3, b4 = boxes[j]
            iy = max(0, min(a2, b2) - max(a1, b1)); ix = max(0, min(a4, b4) - max(a3, b3))
            small = min((a2 - a1) * (a4 - a3), (b2 - b1) * (b4 - b3)) or 1e-12
            if (iy * ix) / small > 0.9:
                if len(paths[i]) >= len(paths[j]):
                    keep[j] = False
                else:
                    keep[i] = False
                    break
    return [p for p, k in zip(paths, keep) if k]


def fetch_metro_planned():
    """在建/规划地铁线路（"十五五"规划明确 2030 年前建成的部分）。

    这些线路多数尚未通车，OSM 里没有稳定的 route relation，只能按
    railway=construction 的 way（按线路名匹配）现抓现拼；崇明线（22号线）
    目前 OSM 连 way 都没有，退化为用已有的车站节点顺次连线做示意。
    数据源不稳定，若某条线抓不到，跳过并提示，不影响其余线路。
    """
    print("▸ 抓取在建/规划地铁线路（Overpass）…")
    specs = [
        ("嘉闵线", "嘉闵线", "#6B7A99", "嘉闵线（2030年前建成）"),
        ("南汇支线", "南汇支线|南汇线", "#8A6D5C", "南汇支线（2030年前建成）"),
        ("21号线", "地铁21号线|轨道交通21号线", "#9A6B7A", "21号线一期及东延伸（2030年前建成）"),
        ("23号线", "地铁23号线|轨道交通23号线", "#6B8A9A", "23号线一期（2030年前建成）"),
        # 19/20 号线：官方色分别为 CMYK(0,42,52,0) 橙红、CMYK(93,0,78,0) 绿，按在建线路惯例降低饱和度
        ("19号线", "地铁19号线|轨道交通19号线", "#D9896E", "19号线（宝山站—虹建路，在建）"),
        ("20号线", "地铁20号线|轨道交通20号线", "#4E9A6A", "20号线一期在建（上海西站—共青森林公园），东延伸规划中"),
    ]
    lines = {}
    for key, name_re, colour, note in specs:
        try:
            # 经纬度框而不是 area：部分镜像没建 area 索引，会静默返回空结果
            d = overpass(f'[out:json][timeout:120];'
                        f'way["railway"]["name"~"{name_re}"](30.65,120.85,31.90,122.25);out geom;')
        except Exception as e:
            print(f"  ⚠ {key} 抓取失败，跳过：{e}")
            continue
        segs = [way_pts(e) for e in d["elements"]]
        paths = _dedup_near([simplify(s, 0.0004) for s in stitch(segs) if len(s) > 1])
        if not paths:
            print(f"  ⚠ {key} 未找到几何数据，跳过")
            continue
        lines[key] = {"colour": colour, "note": note, "paths": [to_latlng(p) for p in paths]}
        print(f"  {key}: {len(paths)} 段")

    # 12号线西延伸：只取已有"12号线" way 里带 construction 标记的新建段
    try:
        d = overpass('[out:json][timeout:120];'
                    'way["railway"]["name"~"12号线"](30.65,120.85,31.90,122.25);out geom;')
        els = [e for e in d["elements"] if e.get("tags", {}).get("construction") == "subway"]
        segs = [way_pts(e) for e in els]
        paths = [simplify(s, 0.0004) for s in stitch(segs) if len(s) > 1]
        if paths:
            lines["12号线西延伸"] = {"colour": "#007B5F", "note": "12号线西延伸（2030年前建成）",
                                   "paths": [to_latlng(p) for p in paths]}
            print(f"  12号线西延伸: {len(paths)} 段")
    except Exception as e:
        print(f"  ⚠ 12号线西延伸 抓取失败，跳过：{e}")

    # 崇明线（22号线）：OSM 尚无完整线路几何，退化为按已知车站节点顺次连线（示意，非精确走向）
    try:
        d = overpass('[out:json][timeout:60];'
                    '(node["railway"="station"]["name"~"^金吉路$|^申江路$|^凌空北路$|'
                    '^长兴岛$|^陈家镇$|^东滩$|^裕安$"](30.9,121.4,31.7,122.0););out;')
        pts = {}
        for e in d["elements"]:
            pts.setdefault(e["tags"]["name"], (e["lat"], e["lon"]))
        order = ["金吉路", "申江路", "高宝路", "凌空北路", "长兴岛", "陈家镇", "东滩", "裕安"]
        if "申江路" in pts and "凌空北路" in pts:
            lat = (pts["申江路"][0] + pts["凌空北路"][0]) / 2
            lon = (pts["申江路"][1] + pts["凌空北路"][1]) / 2
            pts.setdefault("高宝路", (lat, lon))
        path = [[pts[s][0], pts[s][1]] for s in order if s in pts]
        if len(path) >= 2:
            lines["崇明线"] = {"colour": "#6B8F71",
                             "note": "崇明线（2030年前建成，车站间为近似直线示意）",
                             "paths": [path]}
            print(f"  崇明线: {len(path)} 站（示意直线）")
    except Exception as e:
        print(f"  ⚠ 崇明线 抓取失败，跳过：{e}")

    json.dump(lines, open(os.path.join(DATA, "metro_planned.json"), "w"), ensure_ascii=False)
    print(f"  完成 → data/metro_planned.json（{len(lines)} 条）")


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
    if want == "metro_planned":  # 数据不稳定，不纳入 all，需要时单独跑并人工核对
        fetch_metro_planned()
    print("完成。接下来运行 python3 build_map.py 重新生成地图。")
