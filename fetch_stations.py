#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上海轨道交通车站（已开通 + 在建/规划）→ data/stations.json
==========================================================
用法（Python 3 标准库，需联网，Overpass 繁忙时可能要跑十几分钟）：

    python3 fetch_stations.py            # 重新抓取并生成 data/stations.json
    python3 fetch_stations.py --cached   # 只用上次下载的原始数据重新合并（.cache/stations/）

数据来源与优先级：
  · 已开通站：OpenStreetMap 地铁/市域线路 relation 里的停车点（stop_position），
    同名停车点取中心；线路归属取自 relation 的 ref/name。坐标 WGS84，与 metro_lines.json 同源。
    OSM 漏掉的（新开通段）用 Wikidata 的车站坐标补。
  · 在建/规划站名单与所属线路：中文维基百科各线路条目的车站表（表内引用上海市规划资源局
    选线专项规划、环评公示和招标公告）。
  · 在建站坐标：OSM 的在建车站标注 → Wikidata → 维基百科车站条目坐标 →
    data/stations_manual.json（人工补点：道路与在建轨道交点、高德定位等，写明来源）。
    都没有的站不上图，运行结束时列出，补进 stations_manual.json 即可。

输出：[{name, lat, lng, status: open|building, lines: [已开通线路], building: [在建线路], src}]
"""
import collections
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from fetch_geodata import DATA, overpass

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache", "stations")
UA = {"User-Agent": "sh-housing-map/1.0 (station data script)"}
# 用经纬度框而不是 area["name"="上海市"]：部分 Overpass 镜像没建 area 索引，会静默返回空结果
SH_BBOX = "(30.65,120.85,31.90,122.25)"

# 维基百科条目 → 地图上的线路名
WIKI_PAGES = {
    "上海轨道交通12号线": "12号线", "上海轨道交通13号线": "13号线", "上海轨道交通15号线": "15号线",
    "上海轨道交通18号线": "18号线", "上海轨道交通19号线": "19号线", "上海轨道交通20号线": "20号线",
    "上海轨道交通21号线": "21号线", "上海轨道交通23号线": "23号线", "上海机场联络线": "机场联络线",
    "崇明线": "崇明线", "嘉闵线": "嘉闵线", "南汇支线": "南汇支线", "示范区线": "示范区线",
}
# 车站表里出现、但不在上海市域内或属于远期直通的站
NOT_SH = {"太仓", "白云渡路", "十八港路", "嘉善", "枫泾", "金山卫", "上海南"}
LINE_RE = re.compile(r"(\d+号线|浦江线|机场联络线|磁浮)")
# 已开通、但 OSM 还没有线路关系的线：按维基百科车站表标为已开通（机场联络线 2024-12-27 开通）
OPEN_NOT_IN_OSM = {"机场联络线": ["虹桥2号航站楼", "中春路", "景洪路", "三林南", "康桥东",
                                "上海国际旅游度假区", "浦东1号2号航站楼"]}
# Wikidata / 维基百科偶尔给繁体站名
T2S = str.maketrans("華雙吳碼頭東樂園陽濱灣豐莊寶楊匯聯錦臺門龍張長學劉嶺漢臨國際場廣鳳興衛濤橋會館鎮號縣區",
                    "华双吴码头东乐园阳滨湾丰庄宝杨汇联锦台门龙张长学刘岭汉临国际场广凤兴卫涛桥会馆镇号县区")


def line_order(l):
    """线路排序：1号线、2号线 … 18号线，再到浦江线、磁浮、机场联络线等。"""
    m = re.match(r"(\d+)号线", l)
    return (0, int(m[1]), l) if m else (1, 0, l)


def key(s):
    """跨来源对名字用的键："海伦路（在建）"、"上海南站站"、"祁華路站"、"一大会址·新天地" → 去括号、转简体、
    去掉末尾所有"站"字（OSM 写"上海南站"，Wikidata 写"上海南站站"）。"""
    s = re.sub(r"[（(].*?[）)]", "", s).translate(T2S)
    s = re.sub(r"[\s·・]", "", s)
    return re.sub(r"站+$", "", s)


def display(s, wikidata=False):
    """显示用站名：去括号、转简体；Wikidata 标签末尾多一个"站"，去掉。"""
    s = re.sub(r"[（(].*?[）)]", "", s).translate(T2S).strip()
    return re.sub(r"站$", "", s) if wikidata else s


def center(e):
    c = e.get("center", e)
    return c["lon"], c["lat"]


def cached(name, fetch, use_cache):
    path = os.path.join(CACHE, name)
    if use_cache and os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    data = fetch()
    if not data or (isinstance(data, dict) and "elements" in data and not data["elements"]):
        sys.exit(f"{name}：返回空结果（Overpass 镜像可能缺数据），未写缓存，请稍后重跑")
    os.makedirs(CACHE, exist_ok=True)
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return data


def get_json(url, tries=5):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # 维基百科限流（429）时退避重试
            if k == tries - 1:
                raise
            print(f"  {url[:60]}… 失败（{e}），{10 * (k + 1)} 秒后重试")
            time.sleep(10 * (k + 1))


# ──────────────────────────── 下载 ────────────────────────────

def fetch_osm_open():
    print("▸ OSM：已开通线路与停车点…")
    return overpass("[out:json][timeout:300];"
                    f'(relation["route"="subway"]{SH_BBOX};'
                    f'relation["route"~"train|light_rail|monorail"]["name"~"机场联络线|磁浮|浦江线"]{SH_BBOX};)->.r;'
                    ".r out body;"
                    'node(r.r)["public_transport"="stop_position"];out;')


def fetch_osm_building():
    print("▸ OSM：在建车站标注…")
    return overpass("[out:json][timeout:300];("
                    f'nwr["railway"="station"]["name"]{SH_BBOX};'
                    f'nwr["railway"="construction"]["construction"~"station|stop"]["name"]{SH_BBOX};'
                    f'nwr["construction:railway"="station"]["name"]{SH_BBOX};'
                    f'nwr["construction:public_transport"]["name"]{SH_BBOX};'
                    ");out tags center;")


def fetch_wikidata():
    print("▸ Wikidata：车站坐标与线路…")
    q = """SELECT ?sLabel ?coord ?lineLabel ?state WHERE {
      ?line wdt:P31/wdt:P279* wd:Q15079663 .
      { ?line wdt:P361 wd:Q462201 } UNION { ?line wdt:P16 wd:Q462201 } UNION { ?line wdt:P131 wd:Q8686 }
      ?s wdt:P81 ?line . OPTIONAL { ?s wdt:P625 ?coord }
      OPTIONAL { ?s wdt:P5817 ?st . ?st rdfs:label ?state FILTER(lang(?state)='zh') }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "zh-hans,zh-cn,zh,en". } }"""
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q, "format": "json"})
    return get_json(url)["results"]["bindings"]


def fetch_wiki():
    print("▸ 维基百科：在建线路车站表与车站条目坐标…")
    api = "https://zh.wikipedia.org/w/api.php?"
    pages, coords = {}, {}
    for title in WIKI_PAGES:
        d = get_json(api + urllib.parse.urlencode({"action": "parse", "page": title, "prop": "wikitext",
                                                   "format": "json", "redirects": 1}))
        pages[title] = d["parse"]["wikitext"]["*"]
        cont = {}
        while True:  # 条目里链接到的车站页面的坐标
            d = get_json(api + urllib.parse.urlencode({
                "action": "query", "generator": "links", "titles": title, "gpllimit": "max",
                "gplnamespace": 0, "prop": "coordinates", "colimit": "max", "format": "json",
                "redirects": 1, **cont}))
            for pg in d.get("query", {}).get("pages", {}).values():
                if pg.get("coordinates") and re.search(r"站( \(.*\))?$", pg["title"]):
                    c = pg["coordinates"][0]
                    coords[re.sub(r" \(.*\)$", "", pg["title"])] = [c["lon"], c["lat"]]
            if "continue" not in d:
                break
            cont = d["continue"]
            time.sleep(2)
        print(f"  {title}")
        time.sleep(3)
    return {"pages": pages, "coords": coords}


# ──────────────────────────── 解析 ────────────────────────────

STATION_LINK = re.compile(r"\{\{stl\|[^|}]+\|([^}|]+)|\{\{站\|([^}|]+)|"
                          r"\[\[([^\]|]+?)站(?: \([^)]*\))?(?:\|([^\]]+))?\]\]")


def wiki_station_rows(wikitext):
    """维基百科线路条目里"站名"表格的每一行取第一个车站链接，按表内顺序返回。"""
    w = re.sub(r"<ref[^>]*/>", "", wikitext)
    w = re.sub(r"<ref.*?</ref>", "", w, flags=re.S)
    names = []
    for tbl in re.findall(r"\{\|.*?\n\|\}", w, flags=re.S):
        if "站名" not in tbl:
            continue
        for row in tbl.split("\n|-")[1:]:
            for cell in re.split(r"\n[|!]", row):
                m = STATION_LINK.search(cell)
                if m:
                    name = (m[1] or m[2] or m[4] or m[3]).strip()
                    if not re.search(r"File:|铁路$|高铁|线$", name):
                        names.append(key(name))
                    break
    return names


def main(use_cache):
    osm_open = cached("osm_open.json", fetch_osm_open, use_cache)["elements"]
    osm_bld = cached("osm_building.json", fetch_osm_building, use_cache)["elements"]
    wd = cached("wikidata.json", fetch_wikidata, use_cache)
    wiki = cached("wikipedia.json", fetch_wiki, use_cache)
    manual_path = os.path.join(DATA, "stations_manual.json")
    manual = json.load(open(manual_path, encoding="utf-8")) if os.path.exists(manual_path) else {}

    # ── 已开通：relation → 线路名；停车点按站名聚合 ──
    node_lines = collections.defaultdict(set)
    for r in osm_open:
        if r["type"] != "relation":
            continue
        t = r["tags"]
        if t.get("network") and "上海" not in t["network"]:
            continue  # 经纬度框会框进苏州轨道交通 11 号线（花桥换乘），编号撞车
        ref = t.get("ref", "")
        m = LINE_RE.search(t.get("name", "")) or LINE_RE.search(ref + "号线" if ref.isdigit() else ref)
        if not m:
            continue
        for mem in r.get("members", []):
            if mem["type"] == "node":
                node_lines[mem["ref"]].add(m[1])
    open_pts, open_lines = collections.defaultdict(list), collections.defaultdict(set)
    names = {}  # 键 → 显示名
    for e in osm_open:
        if e["type"] == "node" and e["tags"].get("name") and e["id"] in node_lines:
            k = key(e["tags"]["name"])
            open_pts[k].append((e["lon"], e["lat"]))
            open_lines[k] |= node_lines[e["id"]]
            names.setdefault(k, display(e["tags"]["name"]))

    wd_pt, wd_lines, wd_inactive = {}, collections.defaultdict(set), set()
    for x in wd:
        k = key(x["sLabel"]["value"])
        if x.get("state", {}).get("value", "使用中") != "使用中":
            wd_inactive.add(k)  # 退役 / 完工但未启用 / 兴建中：不当作已开通站补
        names.setdefault(k, display(x["sLabel"]["value"], wikidata=True))
        m = LINE_RE.search(x["lineLabel"]["value"].replace("磁浮示范运营线", "磁浮"))
        if m:
            wd_lines[k].add(m[1])
        if "coord" in x:
            lng, lat = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", x["coord"]["value"]).groups()
            wd_pt.setdefault(k, (float(lng), float(lat)))

    stations = {}
    for k, ps in open_pts.items():
        stations[k] = {"name": names.get(k, k), "lng": sum(p[0] for p in ps) / len(ps), "lat": sum(p[1] for p in ps) / len(ps),
                       "status": "open", "lines": sorted(open_lines[k], key=line_order), "building": [], "src": "osm"}

    osm_bld_pt = {}
    for e in osm_bld:
        k = key(e["tags"]["name"])
        if k not in open_pts:
            osm_bld_pt.setdefault(k, center(e))
            names.setdefault(k, display(e["tags"]["name"]))
    for line, ks in OPEN_NOT_IN_OSM.items():
        for k in map(key, ks):
            open_lines[k].add(line)
            if k in stations:
                stations[k]["lines"] = sorted(open_lines[k], key=line_order)
            else:
                p = osm_bld_pt.get(k) or wd_pt.get(k)
                stations[k] = {"name": names.get(k, k), "lng": p[0], "lat": p[1], "status": "open",
                               "lines": [line], "building": [], "src": "osm" if k in osm_bld_pt else "wikidata"}

    # ── 在建：维基百科车站表 ──
    building = collections.defaultdict(set)
    for title, line in WIKI_PAGES.items():
        for k in wiki_station_rows(wiki["pages"][title]):
            if k in NOT_SH or line in open_lines.get(k, ()):
                continue
            building[k].add(line)
    # OSM 还没收录的已开通站（新开通段）：Wikidata 有坐标、属于已开通线路、又不在维基百科在建名单里
    open_line_names = {l for ls in open_lines.values() for l in ls}
    wd_added = []
    for k, ls in wd_lines.items():
        ls = ls & open_line_names
        if k not in stations and k not in building and k in wd_pt and ls and k not in wd_inactive:
            stations[k] = {"name": names.get(k, k), "lng": wd_pt[k][0], "lat": wd_pt[k][1], "status": "open",
                           "lines": sorted(ls, key=line_order), "building": [], "src": "wikidata"}
            wd_added.append(k)
    if wd_added:
        print(f"  OSM 未收录、按 Wikidata 补的已开通站：{'、'.join(wd_added)}")
    wiki_pt = {key(k): tuple(v) for k, v in wiki["coords"].items()}

    missing = []
    for k, ls in sorted(building.items()):
        if k in stations:
            stations[k]["building"] = sorted(ls, key=line_order)
            continue
        for src, pts in (("manual", {m: (v["lng"], v["lat"]) for m, v in manual.items()}),
                         ("osm", osm_bld_pt), ("wikidata", wd_pt), ("wikipedia", wiki_pt)):
            if k in pts:
                stations[k] = {"name": names.get(k, k), "lng": pts[k][0], "lat": pts[k][1], "status": "building",
                               "lines": [], "building": sorted(ls, key=line_order), "src": src}
                break
        else:
            missing.append(f"{k}（{'、'.join(sorted(ls, key=line_order))}）")

    out = []
    for s in sorted(stations.values(), key=lambda s: (s["status"] != "open", s["name"])):
        s["lng"], s["lat"] = round(s["lng"], 6), round(s["lat"], 6)
        out.append(s)
    json.dump(out, open(os.path.join(DATA, "stations.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    n_open = sum(s["status"] == "open" for s in out)
    print(f"  完成 → data/stations.json：已开通 {n_open} 站，在建/规划 {len(out) - n_open} 站"
          f"（其中换乘在建线 {sum(bool(s['building']) for s in out if s['status'] == 'open')} 站）")
    no_line = [s["name"] for s in out if s["status"] == "open" and not s["lines"]]
    if no_line:
        print(f"  ⚠ 已开通但没有线路归属：{no_line}")
    if missing:
        print(f"  ⚠ {len(missing)} 个在建站没有坐标，未上图（补进 data/stations_manual.json）：")
        print("    " + "、".join(missing))


if __name__ == "__main__":
    main(use_cache="--cached" in sys.argv)
