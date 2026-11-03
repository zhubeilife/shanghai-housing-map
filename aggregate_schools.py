#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学区对口数据聚合
================================
用法：

    python3 aggregate_schools.py

读 data/pudong_junior_2025.jsonl（浦东新区 2025 年公办初中招生地段公示的
逐条爬取结果，一行一个"学校 × 地段"），聚合成按街镇索引的
data/school_districts.json，供 build_map.py 内联进地图。

原始数据来自上海市浦东新区教育局公示（shanghai.gov.cn），
每条记录带 source_url。字段说明：

    grade        年级（本批全部是 6，即初中预备班）
    school_name  学校全名
    address      地段地址/门牌（几乎每条都有）
    street_town  街镇全名（"浦兴路街道"、"曹路镇"），与 towns.json 完全一致
    community    小区名，约一半为空（只有门牌号的地段）
    notes        公示备注：统筹安排 / 信息变更 / 双号 / 单号 / 已拆迁 …

聚合规则：
  · 小区名优先，为空时用门牌地址；用 kind 区分（c=小区，a=路段/门牌）
  · 同一学校下重复的地段去重，保留备注非空的合并结果
  · 按街镇汇总：街镇 → 学校列表 → 各地段
"""
import json
import os
import sys
from collections import OrderedDict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "pudong_junior_2025.jsonl")
OUT = os.path.join(HERE, "data", "school_districts.json")

META = {
    "title": "浦东新区2025年公办初中招生地段",
    "district": "浦东",
    "year": 2025,
    "grade": "初中（预备班）",
    "source": "上海市浦东新区教育局 2025 年公办初中招生地段公示",
}


def main():
    if not os.path.exists(RAW):
        sys.exit(f"缺少原始数据：{RAW}")

    with open(os.path.join(HERE, "data", "towns.json"), encoding="utf-8") as f:
        town_names = {t["name"] for t in json.load(f)}

    rows = 0
    skipped = 0
    # town → school → area 名 → area 记录
    towns = OrderedDict()
    urls = set()

    with open(RAW, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows += 1
            town = (d.get("street_town") or "").strip()
            school = (d.get("school_name") or "").strip()
            comm = (d.get("community") or "").strip()
            addr = (d.get("address") or "").strip()
            note = (d.get("notes") or "").strip()
            if not school or not (comm or addr):
                skipped += 1
                continue
            if town not in town_names:
                # 唯一可能出现的问题是街镇名写法变化，宁可报出来也别偷偷丢
                print(f"⚠ 街镇名不在 towns.json 里，已跳过：{town!r}（{school}）")
                skipped += 1
                continue
            urls.add(d.get("source_url", ""))

            area_name, kind = (comm, "c") if comm else (addr, "a")
            # 小区名本身像门牌号的（"川沙路328弄"挂在 community 列），仍按小区处理，
            # 但渲染时能看出是路段
            schools = towns.setdefault(town, OrderedDict())
            areas = schools.setdefault(school, OrderedDict())
            prev = areas.get(area_name)
            if prev is None:
                areas[area_name] = {"n": area_name, "k": kind, **({"note": note} if note else {})}
            elif note and note not in (prev.get("note") or ""):
                # 同一地段出现多次（单号/双号、信息变更），备注合并
                prev["note"] = " / ".join(x for x in (prev.get("note"), note) if x)

    if skipped:
        print(f"跳过 {skipped} 条（街镇未匹配或学校/地段为空）")

    # —— 组装输出 ——
    out_towns = OrderedDict()
    n_school = n_area = 0
    all_schools = set()
    for town, schools in sorted(towns.items(), key=lambda kv: -sum(len(a) for a in kv[1].values())):
        sch_list = []
        for sname, areas in sorted(schools.items(), key=lambda kv: -len(kv[1])):
            alist = list(areas.values())
            sch_list.append({"name": sname, "areas": alist})
            all_schools.add(sname)
            n_area += len(alist)
        out_towns[town] = {
            "n": sum(len(s["areas"]) for s in sch_list),
            "schools": sch_list,
        }
        n_school += len(sch_list)

    meta = dict(META)
    meta.update({
        "rows": rows,
        "towns": len(out_towns),
        "schools": len(all_schools),
        "school_town_pairs": n_school,
        "areas": n_area,
        "urls": sorted(u for u in urls if u),
        "built": date.today().isoformat(),
    })
    out = {"meta": meta, "towns": out_towns}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(OUT) / 1024
    print(f"原始 {rows} 条 → {len(out_towns)} 个街镇 / {len(all_schools)} 所学校 / {n_area} 个地段落点")
    print(f"已生成：{OUT}（{size:.0f} KB）")
    print("公示备注（统筹安排/单双号/已拆迁等）已保留在地段条目上，未做删除。")


if __name__ == "__main__":
    main()
