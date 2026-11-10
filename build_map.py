#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海买房板块地图 · 构建脚本
================================
用法（仅需 Python 3，无第三方依赖）：

    python3 build_map.py

流程：
  1. 读取 boards.py 里的板块房价（← 房价更新时只改这个文件）
  2. 读取 data/ 下的地理数据（区界、街镇边界、环线、地铁）
  3. 读取 data/school_districts.json（学区对口，由 aggregate_schools.py 生成）
  4. 读取 data/newhomes.json（新房楼盘，可选；由新房子项目仓库导出），
     并给每个楼盘标出所属板块单元与街镇
  5. 以"板块点 + 无板块街镇的质心"为种子，在每个区内做
     Voronoi 切分，生成全市板块多边形（板块级细分）
  6. 以 template.html 为模板，内联 Leaflet 与全部数据，
     生成完全自包含的单文件地图：上海买房板块地图.html

想调整板块边界：挪动/增删 boards.py 里的板块点，重新构建即可，
边界会自动重算。地理数据一般不需要动；如需重新抓取
（例如地铁开了新线），运行 fetch_geodata.py 重新生成 data/ 目录。
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import boards  # noqa: E402
from boards import BOARDS, DISTRICT_PRICE  # noqa: E402

VERSION = getattr(boards, "VERSION", "0.0.0")
DATA_DATE = getattr(boards, "DATA_DATE", "")
UPDATE_NOTE = getattr(boards, "UPDATE_NOTE", "")

OUT = os.path.join(HERE, "上海买房板块地图.html")
XSCALE = math.cos(math.radians(31.2))  # 经度按纬度31.2°压缩，近似等距


def load(name):
    with open(os.path.join(HERE, "data", name), encoding="utf-8") as f:
        return json.load(f)


def point_in_ring(lng, lat, ring):
    """射线法。ring 顶点格式为 [lat, lng]。"""
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][1], ring[i][0]
        x2, y2 = ring[i + 1][1], ring[i + 1][0]
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def clip_halfplane(poly, a, b, c):
    """Sutherland–Hodgman：保留满足 a*x+b*y <= c 的部分。poly=[(x,y),...] 不闭合。"""
    out = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        fp = a * p[0] + b * p[1] - c
        fq = a * q[0] + b * q[1] - c
        if fp <= 0:
            out.append(p)
            if fq > 0:
                t = fp / (fp - fq)
                out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
        elif fq <= 0:
            t = fp / (fp - fq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def ring_area(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def build_cells(districts, towns):
    """按区做 Voronoi：种子 = 该区板块点 + 区内没有板块的街镇质心。"""
    # 街镇是否已被板块覆盖：包含板块点，或与同区某板块同名
    #（例如"古美"板块点若恰好落在梅陇镇一侧，也不再给古美路街道生成兜底种子）
    town_has_board = {id(t): False for t in towns}
    board_town = {}   # 板块名 → 所在街镇全名（用于挂学区对口数据）
    for name, dist, lat, lng, *_ in BOARDS:
        for t in towns:
            if t["dist"] == dist and any(point_in_ring(lng, lat, r) for r in t["rings"]):
                town_has_board[id(t)] = True
                board_town[name] = t["name"]
                break
    for t in towns:
        if town_has_board[id(t)]:
            continue
        short = t["name"].replace("街道", "").replace("镇", "").replace("乡", "")
        for bname, dist, *_ in BOARDS:
            if dist == t["dist"] and (short in bname or bname in short):
                town_has_board[id(t)] = True
                board_town.setdefault(bname, t["name"])
                break

    cells = []
    for f in districts["features"]:
        dname = f["properties"]["name"].replace("新区", "").replace("区", "")
        # 种子：板块（带价格） + 无板块街镇质心（用区均价）
        seeds = []
        for name, dist, lat, lng, lo, hi, major, zone in BOARDS:
            if dist == dname:
                seeds.append({"name": name, "lat": lat, "lng": lng,
                              "lo": lo, "hi": hi, "zone": zone, "est": False,
                              "town": board_town.get(name)})
        for t in towns:
            if t["dist"] == dname and not town_has_board[id(t)]:
                seeds.append({"name": t["name"].replace("街道", "").replace("镇", ""),
                              "lat": t["c"][0], "lng": t["c"][1],
                              "lo": None, "hi": None, "zone": "", "est": True,
                              "town": t["name"]})
        if not seeds:
            continue

        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        outer_rings = [[(p[0] * XSCALE, p[1]) for p in poly[0]] for poly in polys]
        pts = [(s["lng"] * XSCALE, s["lat"]) for s in seeds]

        for i, s in enumerate(seeds):
            xi, yi = pts[i]
            parts = []
            for ring in outer_rings:
                poly = ring[:-1] if ring[0] == ring[-1] else ring[:]
                for j, (xj, yj) in enumerate(pts):
                    if j == i:
                        continue
                    # 距 i 更近：2(sj-si)·p <= |sj|²-|si|²
                    a = 2 * (xj - xi)
                    b = 2 * (yj - yi)
                    c = xj * xj + yj * yj - xi * xi - yi * yi
                    poly = clip_halfplane(poly, a, b, c)
                    if len(poly) < 3:
                        break
                if len(poly) >= 3 and ring_area(poly) > 1e-7:
                    parts.append(poly)
            if not parts:
                continue
            area = sum(ring_area(p) for p in parts) / XSCALE  # 换回未压缩 deg²
            rings = [[[round(y, 5), round(x / XSCALE, 5)] for x, y in p] +
                     [[round(p[0][1], 5), round(p[0][0] / XSCALE, 5)]] for p in parts]
            cells.append({"name": s["name"], "dist": dname,
                          "c": [s["lat"], s["lng"]], "a": round(area, 6),
                          "lo": s["lo"], "hi": s["hi"], "zone": s["zone"],
                          "est": s["est"], "tw": s.get("town"), "rings": rings})
    return cells


def build_towns(towns):
    """为街镇级视图构建数据：每个街镇匹配落入其中的板块点，附带价格信息。"""
    result = []
    for t in towns:
        matched = []
        for name, dist, lat, lng, lo, hi, major, zone in BOARDS:
            if dist == t["dist"] and any(point_in_ring(lng, lat, r) for r in t["rings"]):
                matched.append({"name": name, "lo": lo, "hi": hi, "zone": zone})
        # 街镇面积（简易，用 deg²）
        area = 0.0
        for r in t["rings"]:
            pts = [(p[1], p[0]) for p in r]  # [lat,lng] → (lng,lat)
            area += ring_area(pts)
        result.append({
            "name": t["name"].replace("街道", "").replace("镇", "").replace("乡", ""),
            "fullname": t["name"],
            "dist": t["dist"],
            "c": t["c"],
            "a": round(area, 6),
            "rings": t["rings"],
            "boards": matched,
        })
    return result


def main():
    towns = load("towns.json")
    districts = load("districts.json")
    metro = load("metro_lines.json")
    metro_planned = load("metro_planned.json")
    rings = load("ring_roads.json")

    # ── 学区对口（浦东公办初中 2025 公示，由 aggregate_schools.py 聚合）──
    school_path = os.path.join(HERE, "data", "school_districts.json")
    if os.path.exists(school_path):
        school = load("school_districts.json")
        school_meta, school_towns = school["meta"], school["towns"]
        print(f"学区对口：{school_meta['towns']} 个街镇 / {school_meta['schools']} 所学校"
              f"（{school_meta['district']}·{school_meta['year']} {school_meta['grade']}）")
    else:
        school_meta, school_towns = {}, {}
        print("⚠ 没有 data/school_districts.json，本次构建不含学区对口数据")

    # ── Voronoi 板块细分 ──
    cells = build_cells(districts, towns)
    n_board = sum(1 for c in cells if not c["est"])
    print(f"生成板块单元：{len(cells)} 个（板块 {n_board} + 街镇兜底 {len(cells) - n_board}）")
    missing = len(BOARDS) - n_board
    if missing:
        names = {c['name'] for c in cells}
        lost = [b[0] for b in BOARDS if b[0] not in names]
        print(f"⚠ {missing} 个板块点未生成单元（可能落在区界外）：{lost}")

    # ── 街镇级视图（板块匹配到街镇）──
    town_data = build_towns(towns)
    n_matched = sum(1 for t in town_data if t["boards"])
    print(f"街镇级视图：{len(town_data)} 个街镇（{n_matched} 个含有板块价格）")

    # ── 新房楼盘（房天下，由 ../shanghai-ai-housing 的 newhome.py export-map 生成；可选）──
    nh_path = os.path.join(HERE, "data", "newhomes.json")
    if os.path.exists(nh_path):
        nh = load("newhomes.json")
        nh_meta, nh_items = nh.get("meta", {}), nh["items"]
    else:
        nh_meta, nh_items = {}, []
        print("⚠ 没有 data/newhomes.json，本次构建不含新房数据")
    # 归属：cell = 所在板块单元下标，town = 所在街镇下标（落不进为 null）
    n_in_cell = 0
    for it in nh_items:
        lng, lat = it["lng"], it["lat"]
        it["cell"] = next((i for i, c in enumerate(cells)
                           if any(point_in_ring(lng, lat, r) for r in c["rings"])), None)
        it["town"] = next((i for i, t in enumerate(town_data)
                           if any(point_in_ring(lng, lat, r) for r in t["rings"])), None)
        n_in_cell += it["cell"] is not None
    if nh_items:
        n_sold = sum(1 for it in nh_items if it.get("status") == "在售")
        print(f"新房楼盘：{len(nh_items)} 个（在售 {n_sold} / 待售 {len(nh_items) - n_sold}），"
              f"{len(nh_items) - n_in_cell} 个未落入板块单元")

    # ── 街镇界参考线（浅色描边图层，默认关闭）──
    town_lines = [r for t in towns for r in t["rings"]]

    # ── 组装数据段 ──
    for f in districts["features"]:
        f["properties"] = {"name": f["properties"]["name"]}
    from datetime import date
    meta = {"version": VERSION, "dataDate": DATA_DATE,
            "built": date.today().isoformat(), "note": UPDATE_NOTE}
    j = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    data_js = (
        f"const MAPMETA={j(meta)};\n"
        f"const CELLS={j(cells)};\n"
        f"const TOWNS={j(town_data)};\n"
        f"const TOWNLINES={j(town_lines)};\n"
        f"const DISTRICTS={j(districts)};\n"
        f"const METRO={j(metro)};\n"
        f"const METRO_PLANNED={j(metro_planned)};\n"
        f"const RINGS={j(rings)};\n"
        f"const DIST_PRICE={j(DISTRICT_PRICE)};\n"
        f"const SCHOOLMETA={j(school_meta)};\n"
        f"const SCHOOLS={j(school_towns)};\n"
        f"const NEWHOMEMETA={j(nh_meta)};\n"
        f"const NEWHOMES={j(nh_items)};\n"
    )

    # ── 内联 Leaflet 与数据，输出单文件 ──
    with open(os.path.join(HERE, "vendor", "leaflet.css"), encoding="utf-8") as f:
        lcss = f.read()
    with open(os.path.join(HERE, "vendor", "leaflet.js"), encoding="utf-8") as f:
        ljs = f.read().replace("//# sourceMappingURL=leaflet.js.map", "")
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        html = f.read()

    for ph in ("/*__LEAFLET_CSS__*/", "/*__LEAFLET_JS__*/", "/*__DATA__*/"):
        if ph not in html:
            sys.exit(f"模板缺少占位符 {ph}")
    html = (html.replace("/*__LEAFLET_CSS__*/", lcss)
                .replace("/*__LEAFLET_JS__*/", ljs)
                .replace("/*__DATA__*/", data_js))

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成：{OUT}（{os.path.getsize(OUT) / 1024:.0f} KB）")

    # ── 同时输出 index.html 和 dist/index.html（供托管平台部署）──
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    dist_dir = os.path.join(HERE, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    with open(os.path.join(dist_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print(f"版本 v{VERSION} · 数据 {DATA_DATE} · 构建 {meta['built']}")
    print("双击用浏览器打开即可；发给别人也只需要这一个 HTML 文件。")
    print(f"部署用文件：dist/index.html")

    # ── 追加 CHANGELOG（同一版本号重复构建不会重复记录）──
    log = os.path.join(HERE, "CHANGELOG.md")
    entry = (f"## v{VERSION}（{meta['built']}）\n\n"
             f"- 数据月份：{DATA_DATE}\n- {UPDATE_NOTE or '（无说明）'}\n\n")
    old = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            old = f.read()
    if f"## v{VERSION}（" not in old:
        head_mark = "# 更新日志\n\n"
        body = old[len(head_mark):] if old.startswith(head_mark) else old
        with open(log, "w", encoding="utf-8") as f:
            f.write(head_mark + entry + body)
        print(f"已记录到 CHANGELOG.md（v{VERSION}）")


if __name__ == "__main__":
    main()