#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海买房板块地图 · 构建脚本
================================
用法（仅需 Python 3，无第三方依赖）：

    python3 build_map.py

流程：
  1. 读取 boards.py 里的板块房价（← 房价更新时只改这个文件）
  2. 读取 data/ 下的地理数据（街镇边界、区界、环线、地铁）
  3. 把每个板块价格点匹配到所在街镇（点在多边形内判定）
  4. 以 template.html 为模板，内联 Leaflet 与全部数据，
     生成完全自包含的单文件地图：上海买房板块地图.html

地理数据一般不需要动；如需重新抓取（例如地铁开了新线），
运行 fetch_geodata.py 重新生成 data/ 目录。
"""
import json
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


def main():
    towns = load("towns.json")
    districts = load("districts.json")
    metro = load("metro_lines.json")
    rings = load("ring_roads.json")

    # ── 板块价格点 → 所在街镇 ──
    for t in towns:
        t["boards"] = []
    unmatched = []
    for name, dist, lat, lng, lo, hi, major, zone in BOARDS:
        hit = None
        for t in towns:  # 先在同区找，减少误配
            if t["dist"] == dist and any(point_in_ring(lng, lat, r) for r in t["rings"]):
                hit = t
                break
        if hit is None:
            for t in towns:
                if any(point_in_ring(lng, lat, r) for r in t["rings"]):
                    hit = t
                    break
        if hit:
            hit["boards"].append([name, lo, hi, zone])
        else:
            unmatched.append(name)
    print(f"板块匹配：{len(BOARDS) - len(unmatched)}/{len(BOARDS)}"
          + (f"，未匹配：{unmatched}（检查经纬度是否落在陆地内）" if unmatched else ""))

    # ── 组装数据段 ──
    for f in districts["features"]:
        f["properties"] = {"name": f["properties"]["name"]}
    from datetime import date
    meta = {"version": VERSION, "dataDate": DATA_DATE,
            "built": date.today().isoformat(), "note": UPDATE_NOTE}
    j = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    data_js = (
        f"const MAPMETA={j(meta)};\n"
        f"const TOWNS={j(towns)};\n"
        f"const DISTRICTS={j(districts)};\n"
        f"const METRO={j(metro)};\n"
        f"const RINGS={j(rings)};\n"
        f"const DIST_PRICE={j(DISTRICT_PRICE)};\n"
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
    print(f"版本 v{VERSION} · 数据 {DATA_DATE} · 构建 {meta['built']}")
    print("双击用浏览器打开即可；发给别人也只需要这一个 HTML 文件。")

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
