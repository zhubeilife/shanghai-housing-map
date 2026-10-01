#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用高德定位板块中心坐标
================================
用法（需要环境变量 AMAP_WEBSERVICE_KEY，即高德「Web服务」类型的 Key）：

    python3 locate_boards.py                 # 检查全部板块，打印与现有坐标的偏差
    python3 locate_boards.py 前滩 张江        # 只看指定板块
    python3 locate_boards.py 新板块 --apply   # 写回 boards.py

新增板块时，先在 boards.py 里照格式加一行（坐标随便填 0, 0），
再运行 `python3 locate_boards.py <板块名> --apply` 自动补上坐标。

做法：板块名按 "·" 拆开，每段优先找高德的"热点地名"POI（types=190700，
即商圈/片区中心），找不到时用 LANDMARK 里指定的地标，再不行就地理编码；
多段取平均，最后从高德的 GCJ-02 转成底图用的 WGS-84。
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from boards import BOARDS  # noqa: E402
from boards_edit import rewrite_boards  # noqa: E402
from build_map import point_in_ring  # noqa: E402

# 热点地名查不到或位置不合适的板块片段 → 用这个地标定位（"区|片段": 高德 POI 名）
LANDMARK = {
    "黄浦|老西门": "老西门(地铁站)", "黄浦|豫园": "豫园(地铁站)",
    "静安|大宁": "大宁国际商业广场", "静安|苏河湾": "苏河湾中心绿地", "静安|彭浦": "彭浦新村(地铁站)",
    "徐汇|植物园": "上海植物园", "长宁|西郊": "西郊宾馆", "普陀|万里": "万里街道社区事务受理服务中心",
    "杨浦|同济": "同济大学(四平路校区)", "浦东|源深": "上海源深体育中心", "浦东|竹园": "竹园商贸区",
    "浦东|世博滨江": "世博公园", "浦东|航头": "鹤沙航城", "闵行|金汇": "金汇花园",
    "宝山|上大": "上海大学(宝山校区)", "奉贤|海湾": "海湾镇人民政府", "金山|金山新城": "上海市金山区人民政府",
}
SKIP = {"大虹桥"}  # 只是标签，不参与定位
DIST_FULL = {"浦东": "浦东新区"}


def amap(url, **params):
    key = os.environ.get("AMAP_WEBSERVICE_KEY") or os.environ.get("AMAP_KEY")
    if not key:
        sys.exit("未找到 AMAP_WEBSERVICE_KEY 环境变量（高德控制台创建的「Web服务」类型 Key）。")
    params["key"] = key
    secret = os.environ.get("AMAP_WEBSERVICE_SECRET")  # Key 开启了数字签名时需要
    if secret:
        raw = "&".join(f"{k}={params[k]}" for k in sorted(params))
        params["sig"] = hashlib.md5((raw + secret).encode()).hexdigest()
    for attempt in range(5):
        time.sleep(0.35 + 0.4 * attempt)  # 免费 Key 的 QPS 很低
        with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=20) as r:
            d = json.load(r)
        if d.get("status") == "1":
            return d
        if "QPS" not in d.get("info", "") and "TOO_FREQUENT" not in d.get("info", ""):
            break
    sys.exit(f"高德 API 错误：{d.get('info')}")


def locate(part, dist):
    """返回 (GCJ-02 经度, 纬度, 来源说明)。"""
    dfull = DIST_FULL.get(dist, dist + "区")
    kw = LANDMARK.get(f"{dist}|{part}")
    pois = amap("https://restapi.amap.com/v5/place/text", keywords=kw or part, region="上海",
                city_limit="true", page_size=10, **({} if kw else {"types": "190700"}))["pois"]
    if kw:
        hits = [p for p in pois if p["name"] == kw] or pois[:1]
    else:
        hits = [p for p in pois if dfull in p.get("adname", "") and part in p["name"]]
    if hits:
        lng, lat = map(float, hits[0]["location"].split(","))
        return lng, lat, ("地标 " if kw else "热点地名 ") + hits[0]["name"]
    g = amap("https://restapi.amap.com/v3/geocode/geo", address=f"上海市{dfull}{part}", city="上海")["geocodes"]
    if not g:
        sys.exit(f"高德查不到「{part}」，请在 LANDMARK 里给它指定一个地标。")
    lng, lat = map(float, g[0]["location"].split(","))
    return lng, lat, "地理编码 " + g[0]["formatted_address"]


def gcj2wgs(lng, lat):
    """GCJ-02（高德、贝壳等国内图商坐标）→ WGS-84，误差约 1–2 米。"""
    a, ee = 6378245.0, 0.00669342162296594323
    x, y = lng - 105, lat - 35
    dlat = (-100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
            + (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
            + (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
            + (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3)
    dlng = (300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
            + (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
            + (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
            + (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3)
    rad = lat / 180 * math.pi
    m = 1 - ee * math.sin(rad) ** 2
    dlat = dlat * 180 / ((a * (1 - ee)) / (m * math.sqrt(m)) * math.pi)
    dlng = dlng * 180 / (a / math.sqrt(m) * math.cos(rad) * math.pi)
    return lng - dlng, lat - dlat


def km(lat1, lng1, lat2, lng2):
    return math.hypot((lat1 - lat2) * 111.0, (lng1 - lng2) * 111.0 * math.cos(math.radians(31)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="只处理这些板块（默认全部）")
    ap.add_argument("--apply", action="store_true", help="写回 boards.py")
    a = ap.parse_args()

    known = {b[0] for b in BOARDS}
    unknown = [n for n in a.names if n not in known]
    if unknown:
        sys.exit("boards.py 里没有这些板块：" + "、".join(unknown))
    towns = json.load(open(os.path.join(HERE, "data", "towns.json"), encoding="utf-8"))

    updates = {}
    for name, dist, lat, lng, *_ in BOARDS:
        if a.names and name not in a.names:
            continue
        pts = [locate(p, dist) for p in name.split("·") if p not in SKIP]
        glng = sum(p[0] for p in pts) / len(pts)
        glat = sum(p[1] for p in pts) / len(pts)
        wlng, wlat = gcj2wgs(glng, glat)
        inside = any(t["dist"] == dist and any(point_in_ring(wlng, wlat, r) for r in t["rings"]) for t in towns)
        moved = km(lat, lng, wlat, wlng)
        updates[name] = {"lat": wlat, "lng": wlng}
        flag = "" if inside else "  ⚠ 不在本区街镇边界内，请检查或在 LANDMARK 里指定地标"
        print(f"{name:<10}({wlat:.4f}, {wlng:.4f})  偏离现值 {moved:5.2f} km  ← {'；'.join(p[2] for p in pts)}{flag}")

    if a.apply:
        print(f"\n已改写 boards.py：{rewrite_boards(updates)} 行。运行 python3 build_map.py 重新生成地图。")


if __name__ == "__main__":
    main()
