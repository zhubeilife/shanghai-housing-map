#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用贝壳数据更新板块参考价
================================
用法（需要本机装好贝壳 CLI `beike` 并已登录，见 README）：

    python3 update_prices.py              # 抓取并打印新旧对比，不改文件
    python3 update_prices.py --apply      # 确认后写回 boards.py（并把 DATA_DATE 改成本月）

做法：对每个板块名（"·" 分隔的每一段）调用
    beike buy rank -c 上海 -q <板块> --rank-type resblock
取"上榜小区"里确实位于 <区><板块>商圈 的小区挂牌均价，
用 25%–75% 分位作为该板块的参考价区间。上榜小区少于 --min-n 个
（默认 10）的板块不改，保留旧值并在行尾注明。

注意：
- 这是挂牌价，不是成交价。小区榜偏向成交活跃的小区，次新房、豪宅偏多的
  板块会被低估（例如贝壳把前滩并进"杨思前滩"，所以前滩不会被自动更新）。
- 贝壳 Key 每天大约只能调用 150 次，全量 136 次接近上限。结果按月缓存在
  .cache/beike/<年-月>/，中途触顶就隔天再跑，会从断点继续。
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from boards import BOARDS, DATA_DATE  # noqa: E402
from boards_edit import rewrite_boards, set_constant  # noqa: E402

KEEP_NOTE = "贝壳商圈对不上或样本不足，沿用 {date} 数据"
QUOTA_HINT = "service temporarily unavailable"


def fetch_rank(part, cache_dir, gap):
    path = os.path.join(cache_dir, f"rank_{part}.json")
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))["data"]
    cmd = ["beike", "buy", "rank", "-c", "上海", "-q", part, "--rank-type", "resblock"]
    for wait in (0, 60, 180):  # 偶发限流时退避重试
        time.sleep(gap + wait)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            d = json.loads(p.stdout)
        except ValueError:
            d = {"ok": False, "error": (p.stdout + p.stderr).strip()[-200:]}
        if d.get("ok"):
            json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            return d["data"]
        print(f"  {part}: {d.get('error')}", flush=True)
    return None


def listing_avgs(text, dist, part):
    """上榜小区中位于 <区><板块>商圈 的挂牌均价（万/㎡）。查不到该板块时贝壳会返回全城热门榜，靠这里过滤掉。"""
    out = []
    for m in re.findall(r'\{"摘要信息": .*?\}\}', text):
        s = json.loads(m)["摘要信息"]
        where = s.get("区位交通", "").replace("新区", "").replace("区", "", 1)
        price = re.search(r"挂牌均价([\d.]+)万", s.get("市场行情", ""))
        if price and where.startswith(f"位于{dist}{part}"):
            out.append(float(price.group(1)))
    return out


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(k)
    return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="写回 boards.py")
    ap.add_argument("--month", default=datetime.date.today().strftime("%Y-%m"), help="缓存分组与新的 DATA_DATE，默认本月")
    ap.add_argument("--min-n", type=int, default=10, help="至少多少个上榜小区才更新，默认 10")
    ap.add_argument("--gap", type=float, default=8, help="两次贝壳请求的间隔秒数，默认 8")
    a = ap.parse_args()

    cache_dir = os.path.join(HERE, ".cache", "beike", a.month)
    os.makedirs(cache_dir, exist_ok=True)

    parts = sorted({p for b in BOARDS for p in b[0].split("·")})
    todo = [p for p in parts if not os.path.exists(os.path.join(cache_dir, f"rank_{p}.json"))]
    if todo:
        print(f"贝壳小区榜：已缓存 {len(parts) - len(todo)}/{len(parts)}，还需请求 {len(todo)} 次（约 {len(todo) * a.gap / 60:.0f} 分钟）")
    for i, p in enumerate(todo, 1):
        if fetch_rank(p, cache_dir, a.gap) is None:
            print(f"\n第 {i}/{len(todo)} 个请求失败，多半是当天限额用完了。已抓到的都在缓存里，明天再运行同一命令会接着抓。")
            sys.exit(2)
        if i % 20 == 0:
            print(f"  已请求 {i}/{len(todo)}", flush=True)

    updates, rows, kept = {}, [], []
    for name, dist, lat, lng, lo, hi, *_ in BOARDS:
        vals = []
        for part in name.split("·"):
            text = json.load(open(os.path.join(cache_dir, f"rank_{part}.json"), encoding="utf-8"))["data"]
            vals += listing_avgs(text, dist, part)
        if len(vals) >= a.min_n:
            nlo, nhi = round(pct(vals, .25), 1), round(pct(vals, .75), 1)
            updates[name] = {"lo": nlo, "hi": nhi, "note": None}
            rows.append(((nlo + nhi) / (lo + hi) - 1, name, dist, lo, hi, nlo, nhi, len(vals)))
        else:
            kept.append((name, len(vals)))

    rows.sort()
    print(f"\n{'板块':<10}{'区':<4}{'现价':>10}  →{'贝壳':>10}  变化  上榜小区")
    for d, name, dist, lo, hi, nlo, nhi, n in rows:
        print(f"{name:<10}{dist:<4}{f'{lo:g}-{hi:g}':>10}  →{f'{nlo:g}-{nhi:g}':>10}  {d:+4.0%}  {n}")
    if rows:
        print(f"\n可更新 {len(rows)} 个板块，变化中位数 {statistics.median(r[0] for r in rows):+.0%}")
    print(f"保留旧值 {len(kept)} 个（上榜小区 < {a.min_n}）：" + "、".join(f"{n}({k})" for n, k in kept))

    if not a.apply:
        print("\n确认无误后加 --apply 写回 boards.py。")
        return
    note = KEEP_NOTE.format(date=DATA_DATE)
    src = open(os.path.join(HERE, "boards.py"), encoding="utf-8").read()
    for name, _ in kept:  # 已有说明（例如更早的沿用记录）的保持不动
        if not re.search(rf'\("{re.escape(name)}",.*\),\s+#', src):
            updates[name] = {"note": note}
    n = rewrite_boards(updates)
    set_constant("DATA_DATE", a.month)
    print(f"\n已改写 boards.py：{n} 行，DATA_DATE = {a.month}。"
          "记得手动改 VERSION 和 UPDATE_NOTE，再运行 python3 build_map.py。")


if __name__ == "__main__":
    main()
