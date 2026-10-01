# -*- coding: utf-8 -*-
"""update_prices.py 与 locate_boards.py 共用：按板块名改写 boards.py 里的字段。"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BOARDS_PY = os.path.join(HERE, "boards.py")

# ("名", "区", lat, lng, lo, hi, major, "环线")  # 可选行尾注释
ROW = re.compile(r'^(\s*)\("([^"]+)", "([^"]+)", ([\d.]+), ([\d.]+), ([\d.]+), ([\d.]+), (\d), "([^"]*)"\),(.*)$')


def rewrite_boards(updates, path=BOARDS_PY):
    """updates: {板块名: {"lat","lng","lo","hi","note"} 中的任意几项}。

    note 为 None 时删掉行尾注释，为字符串时替换成该注释，不给则保持原样。
    返回实际改动的板块数。
    """
    lines = open(path, encoding="utf-8").read().split("\n")
    changed = 0
    for i, line in enumerate(lines):
        m = ROW.match(line)
        if not m or m.group(2) not in updates:
            continue
        u = updates[m.group(2)]
        ind, name, dist, lat, lng, lo, hi, major, zone, tail = m.groups()
        lat = f'{u["lat"]:.4f}' if "lat" in u else lat
        lng = f'{u["lng"]:.4f}' if "lng" in u else lng
        lo = f'{u["lo"]:g}' if "lo" in u else lo
        hi = f'{u["hi"]:g}' if "hi" in u else hi
        if "note" in u:
            tail = f"  # {u['note']}" if u["note"] else ""
        new = f'{ind}("{name}", "{dist}", {lat}, {lng}, {lo}, {hi}, {major}, "{zone}"),{tail}'
        if new != line:
            lines[i] = new
            changed += 1
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return changed


def set_constant(name, value, path=BOARDS_PY):
    """改写 boards.py 顶部形如 NAME = "..." 的常量，保留行尾注释。"""
    s = open(path, encoding="utf-8").read()
    s = re.sub(rf'^({name} = )"[^"\n]*"', lambda m: f'{m.group(1)}"{value}"', s, count=1, flags=re.M)
    open(path, "w", encoding="utf-8").write(s)
