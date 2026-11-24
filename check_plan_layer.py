#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查「规划用地（控规）」图层还能不能用
================================
控规瓦片是浏览器打开地图时实时从规划资源局服务器拉的，数据更新不用我们管；
这个脚本只检查会让图层出问题的两类变化：

  1. 瓦片服务：元数据是否还能访问、坐标系是否仍是 EPSG:3857、
     陆家嘴的样例瓦片是否还是正常 PNG（不是空白或报错页）
  2. 图例配色：template.html 里 PLAN_LEGEND 的颜色是否还和官方控规图例
     （shanghai.tianditu.gov.cn/xg/js/config.js）一致

瓦片地址和图例都从 template.html 读取，不在这里重复维护。只用 Python 3 标准库。

用法：

    python3 check_plan_layer.py

退出码：0 全部正常；1 有项目不通过（按提示改 template.html 后重新构建）。
"""
import json
import math
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
LEGEND_JS = "https://shanghai.tianditu.gov.cn/xg/js/config.js"
SAMPLE = (121.505, 31.235, 15)   # 陆家嘴：lng, lat, zoom
UA = "Mozilla/5.0 (shanghai-housing-map check)"

fails = []


def report(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f"：{detail}" if detail else ""))
    if not ok:
        fails.append(name)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def read_template():
    html = open(TEMPLATE, encoding="utf-8").read()
    m = re.search(r"L\.tileLayer\('(https://[^']*planblock[^']*)'", html)
    if not m:
        sys.exit("template.html 里找不到控规瓦片地址（L.tileLayer('...planblock...')）")
    tile_url = m.group(1)
    m = re.search(r"const PLAN_LEGEND = \[(.*?)\];", html, re.S)
    if not m:
        sys.exit("template.html 里找不到 PLAN_LEGEND")
    legend = re.findall(r"\['([^']+)','(#[0-9a-fA-F]{6})','([A-Za-z0-9]+)'\]", m.group(1))
    return tile_url, legend


def check_service(tile_url):
    print("瓦片服务")
    base = tile_url.split("/tile/")[0]
    try:
        status, _, body = get(base + "?f=json")
        meta = json.loads(body)
        wkid = (meta.get("spatialReference") or {}).get("latestWkid")
        report("元数据可访问", status == 200 and "error" not in meta, meta.get("error", {}).get("message", ""))
        report("坐标系为 EPSG:3857", wkid == 3857, f"实际 {wkid}")
        report("是缓存瓦片服务", bool(meta.get("singleFusedMapCache")))
    except Exception as e:   # 网络、JSON 解析等
        report("元数据可访问", False, repr(e)[:160])

    lng, lat, z = SAMPLE
    n = 2 ** z
    x = int((lng + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    url = tile_url.replace("{z}", str(z)).replace("{y}", str(y)).replace("{x}", str(x))
    try:
        status, ctype, body = get(url)
        is_png = body[:8] == b"\x89PNG\r\n\x1a\n"
        # 空白瓦片只有几百字节；陆家嘴 15 级正常约 7–8 KB
        report("样例瓦片是 PNG", status == 200 and is_png, f"{status} {ctype}")
        report("样例瓦片有内容（>2 KB）", len(body) > 2048, f"{len(body)} 字节")
    except Exception as e:
        report("样例瓦片可下载", False, repr(e)[:160])


def check_legend(legend):
    print(f"图例配色（{len(legend)} 项，对照 {LEGEND_JS}）")
    try:
        _, _, body = get(LEGEND_JS)
    except Exception as e:
        report("官方图例可访问", False, repr(e)[:160])
        return
    text = body.decode("utf-8", "replace")
    official = {}
    for name, code, r, g, b in re.findall(
            r'\{name:"([^"]+)",code:"([A-Za-z0-9]+)",r:(\d+),g:(\d+),b:(\d+)\}', text):
        official.setdefault(code, (name, "#%02x%02x%02x" % (int(r), int(g), int(b))))
    if not official:
        report("官方图例可解析", False, "config.js 里没找到 {name,code,r,g,b} 配置，页面结构可能变了")
        return
    for label, color, code in legend:
        if code not in official:
            report(f"{label}（{code}）", False, "官方图例里已没有这个代码")
            continue
        oname, ocolor = official[code]
        report(f"{label}（{code} {oname}）", color.lower() == ocolor,
               "" if color.lower() == ocolor else f"模板 {color}，官方 {ocolor}")


def main():
    tile_url, legend = read_template()
    if not legend:
        sys.exit("PLAN_LEGEND 解析为空：每项应为 ['名称','#rrggbb','用地代码']")
    check_service(tile_url)
    check_legend(legend)
    print()
    if fails:
        print(f"❌ {len(fails)} 项不通过。服务问题：到 https://shanghai.tianditu.gov.cn/xg/map.html "
              "看页面源码里的新瓦片地址；配色问题：按提示改 template.html 的 PLAN_LEGEND。改完运行 build_map.py。")
        sys.exit(1)
    print("✅ 控规图层正常")


if __name__ == "__main__":
    main()
