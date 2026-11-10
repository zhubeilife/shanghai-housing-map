#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试
================================
用法：

    python3 tests/regression.py            # 全量（含与上一次提交的基线对比）
    python3 tests/regression.py --no-base  # 跳过基线对比（没有 git / 只想快点跑）

覆盖：
  A. 构建链路      aggregate_schools.py / build_map.py 能否跑通、输出是否一致
  B. 数据不变量    与 HEAD 版本构建出的地图逐项比对（板块/街镇/环线/地铁/区价）
  C. 学区聚合      36 街镇全覆盖、去重、确定性（两次跑字节一致）
  D. 降级          删掉 school_districts.json / newhomes.json 后仍能构建，且不渲染对应区块
  E. 浏览器行为    无头 Chrome 实际打开地图：视图切换 / 各图层开关 / 标签阈值 /
                   价位筛选 / 弹窗内容 / 学区折叠展开 / 新房图层，并收集 JS 报错

需要本机有 Chrome（或设置环境变量 CHROME 指向可执行文件）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from json import JSONDecoder

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "上海买房板块地图.html")

PASS, FAIL, SKIP = [], [], []


def ok(name, cond, detail=None):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name}" + (f"  ← {detail}" if detail is not None else ""))
    return cond


def skip(name, why):
    SKIP.append(f"{name}（{why}）")


def section(t):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def build(cwd, expect_ok=True):
    r = sh([sys.executable, "build_map.py"], cwd=cwd)
    if expect_ok and r.returncode != 0:
        ok(f"构建 {os.path.basename(cwd) or cwd}", False, r.stdout[-800:] + r.stderr[-800:])
        return None
    return r


def extract(html, name):
    """从生成的 HTML 里取出 const NAME=<json> 的 JSON 值。"""
    i = html.find(f"const {name}=")
    if i < 0:
        return None
    i += len(f"const {name}=")
    for _ in range(2000):
        try:
            return JSONDecoder().raw_decode(html, i)[0]
        except json.JSONDecodeError:
            i += 1
    raise ValueError(name)


# ────────────────────────── A. 构建链路 ──────────────────────────
def test_build():
    section("A. 构建链路")
    r = build(ROOT)
    ok("build_map.py 退出码 0", r is not None and r.returncode == 0,
       r.stderr[-500:] if r else "构建失败")
    ok("构建日志包含学区数据统计", r is not None and "学区对口：" in r.stdout,
       r.stdout if r else "")
    ok("生成的文件存在", os.path.exists(OUT))
    outs = [OUT, os.path.join(ROOT, "index.html"), os.path.join(ROOT, "dist", "index.html")]
    ok("三个产物都存在", all(os.path.exists(p) for p in outs),
       [p for p in outs if not os.path.exists(p)])
    bodies = []
    for p in outs:
        with open(p, encoding="utf-8") as f:
            bodies.append(f.read())
    ok("三个产物内容完全一致", len(set(bodies)) == 1)
    html = bodies[0]
    ok("模板占位符已全部替换",
       not any(ph in html for ph in ("/*__LEAFLET_CSS__*/", "/*__LEAFLET_JS__*/", "/*__DATA__*/")))
    ok("Leaflet 已内联", "L.version" in html or "leaflet" in html.lower())
    ok("未残留 sourceMappingURL", "sourceMappingURL" not in html)
    size = len(html.encode("utf-8")) / 1024
    ok("单文件体积在 0.5–3 MB 之间（保持可分享）", 500 < size < 3000, f"{size:.0f} KB")
    print(f"   产物 {size:.0f} KB")
    return html


# ────────────────────────── C. 学区聚合 ──────────────────────────
def test_aggregate():
    section("C. 学区聚合")
    path = os.path.join(ROOT, "data", "school_districts.json")
    raw = os.path.join(ROOT, "data", "pudong_junior_2025.jsonl")
    ok("原始 jsonl 存在", os.path.exists(raw))
    first = open(path, "rb").read() if os.path.exists(path) else b""
    r = sh([sys.executable, "aggregate_schools.py"], cwd=ROOT)
    ok("aggregate_schools.py 退出码 0", r.returncode == 0, r.stderr[-500:])
    second = open(path, "rb").read()
    ok("两次聚合结果字节一致（确定性）", first == second)

    data = json.loads(second)
    meta, towns = data["meta"], data["towns"]
    with open(os.path.join(ROOT, "data", "towns.json"), encoding="utf-8") as f:
        town_names = {t["name"] for t in json.load(f)}
    ok("街镇数与浦东街镇总数一致（36）", meta["towns"] == 36, meta["towns"])
    ok("所有街镇名都在 towns.json 里", set(towns) <= town_names, sorted(set(towns) - town_names))
    ok("meta.towns 与实际键数一致", meta["towns"] == len(towns))
    ok("meta.areas 与逐条累加一致",
       meta["areas"] == sum(len(s["areas"]) for t in towns.values() for s in t["schools"]),
       (meta["areas"], sum(len(s["areas"]) for t in towns.values() for s in t["schools"])))
    ok("meta.schools 为去重学校数",
       meta["schools"] == len({s["name"] for t in towns.values() for s in t["schools"]}),
       meta["schools"])
    ok("街镇内无重名地段",
       all(len({a["n"] for a in s["areas"]}) == len(s["areas"])
           for t in towns.values() for s in t["schools"]))
    ok("地段 kind 只有 c/a",
       {a["k"] for t in towns.values() for s in t["schools"] for a in s["areas"]} == {"c", "a"})
    ok("保留原始公示链接", any("shanghai.gov.cn" in u for u in meta["urls"]), meta["urls"])
    ok("备注字段未被批量删除", any(a.get("note") for t in towns.values()
                              for s in t["schools"] for a in s["areas"]))
    notes = {a.get("note") for t in towns.values() for s in t["schools"] for a in s["areas"]
             if a.get("note")}
    ok("已拆迁地段保留（与公示一致）",
       any("拆迁" in n or "动迁" in n for n in notes), sorted(notes)[:5])
    print(f"   {meta['towns']} 街镇 / {meta['schools']} 校 / {meta['areas']} 地段 / "
          f"{os.path.getsize(path)/1024:.0f} KB")
    return data


# ────────────────────────── B. 与基线比对 ──────────────────────────
def test_baseline(html_now):
    section("B. 数据不变量（对比 HEAD 基线构建）")
    tmp = tempfile.mkdtemp(prefix="baseline-")
    wt = os.path.join(tmp, "wt")
    r = sh(["git", "worktree", "add", "--detach", wt, "HEAD"], cwd=ROOT)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        skip("基线对比", "git worktree 失败：" + r.stderr.strip()[:120])
        return
    try:
        rb = build(wt)
        if rb is None or rb.returncode != 0:
            skip("基线对比", "基线构建失败")
            return
        base = open(os.path.join(wt, "上海买房板块地图.html"), encoding="utf-8").read()
        now = html_now

        for name, keys in (
            ("CELLS", ("name", "dist", "c", "a", "lo", "hi", "zone", "est", "rings")),
            ("TOWNS", ("name", "fullname", "dist", "c", "a", "rings", "boards")),
            ("DIST_PRICE", None),
            ("RINGS", None),
            ("METRO", None),
            ("METRO_PLANNED", None),
            ("DISTRICTS", None),
        ):
            a, b = extract(base, name), extract(now, name)
            if keys:
                a = [{k: x.get(k) for k in keys} for x in a]
                b = [{k: x.get(k) for k in keys} for x in b]
            same = a == b
            ok(f"{name} 与基线完全一致", same,
               f"基线 {len(a)} → 现在 {len(b)}" if isinstance(a, list) else "内容不同")

        ma, mb = extract(base, "MAPMETA"), extract(now, "MAPMETA")
        # 基线 = HEAD，改完还没提交时 HEAD 版本更旧；提交后两者同版本，
        # 所以这里只要求"不低于"，不能要求"必须不同"
        ver = lambda v: [int(x) for x in str(v).split(".")]
        ok("版本号不低于基线", ver(mb["version"]) >= ver(ma["version"]),
           f"{ma['version']} → {mb['version']}")
        ok("数据月份未被动", ma["dataDate"] == mb["dataDate"], (ma["dataDate"], mb["dataDate"]))
        ok("产物包含 SCHOOLS / SCHOOLMETA 常量",
           extract(now, "SCHOOLS") is not None and extract(now, "SCHOOLMETA") is not None)
        ok("产物包含 NEWHOMES / NEWHOMEMETA 常量",
           extract(now, "NEWHOMES") is not None and extract(now, "NEWHOMEMETA") is not None)
        same = ma["version"] == mb["version"]
        print(f"   基线 v{ma['version']} → 当前 v{mb['version']}"
              + ("（同版本，说明工作区已提交）" if same else ""))
    finally:
        sh(["git", "worktree", "remove", "--force", wt], cwd=ROOT)
        shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────── D. 降级 ──────────────────────────
def test_no_school_file():
    section("D. 无学区数据时降级")
    tmp = tempfile.mkdtemp(prefix="noschool-")
    try:
        for f in ("build_map.py", "boards.py", "template.html"):
            shutil.copy2(os.path.join(ROOT, f), tmp)
        shutil.copytree(os.path.join(ROOT, "vendor"), os.path.join(tmp, "vendor"))
        os.makedirs(os.path.join(tmp, "data"))
        for f in os.listdir(os.path.join(ROOT, "data")):
            if f != "school_districts.json":
                shutil.copy2(os.path.join(ROOT, "data", f), os.path.join(tmp, "data", f))
        r = build(tmp)
        ok("缺 school_districts.json 仍构建成功", r is not None and r.returncode == 0,
           r.stderr[-400:] if r else "构建失败")
        if r is not None and r.returncode == 0:
            ok("构建日志给出警告并继续", "没有 data/school_districts.json" in r.stdout, r.stdout)
            h = open(os.path.join(tmp, "上海买房板块地图.html"), encoding="utf-8").read()
            ok("SCHOOLS 为空对象", extract(h, "SCHOOLS") == {})
            ok("SCHOOLMETA 为空对象", extract(h, "SCHOOLMETA") == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_newhome_file():
    section("D. 无新房数据时降级")
    tmp = tempfile.mkdtemp(prefix="nonh-")
    try:
        for f in ("build_map.py", "boards.py", "template.html"):
            shutil.copy2(os.path.join(ROOT, f), tmp)
        shutil.copytree(os.path.join(ROOT, "vendor"), os.path.join(tmp, "vendor"))
        os.makedirs(os.path.join(tmp, "data"))
        for f in os.listdir(os.path.join(ROOT, "data")):
            if f != "newhomes.json":
                shutil.copy2(os.path.join(ROOT, "data", f), os.path.join(tmp, "data", f))
        r = build(tmp)
        ok("缺 newhomes.json 仍构建成功", r is not None and r.returncode == 0,
           r.stderr[-400:] if r else "构建失败")
        if r is not None and r.returncode == 0:
            ok("构建日志给出警告并继续", "没有 data/newhomes.json" in r.stdout, r.stdout)
            h = open(os.path.join(tmp, "上海买房板块地图.html"), encoding="utf-8").read()
            ok("NEWHOMES 为空数组", extract(h, "NEWHOMES") == [])
            ok("NEWHOMEMETA 为空对象", extract(h, "NEWHOMEMETA") == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────── E. 浏览器行为 ──────────────────────────
CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


def chrome():
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


BROWSER_JS = r"""
<script>
(function(){
  window.__err = [];
  window.onerror = (m, s, l) => { window.__err.push(m + ' @' + l); };
  const R = { pass: [], fail: [], info: {} };
  const ok = (n, c, d) => (c ? R.pass : R.fail).push(n + (c || d === undefined ? '' : '  ← ' + JSON.stringify(d)));
  const later = (ms, fn) => setTimeout(() => {
    try { fn(); }
    catch (e) { R.fail.push('测试脚本异常：' + e.message); done(); }
  }, ms);
  const ready = () => {
    try { return typeof map !== 'undefined' && typeof townPolys !== 'undefined'
               && typeof cellPolys !== 'undefined' && cellPolys.length > 0; }
    catch (e) { return false; }
  };

  let tries = 0;
  (function wait(){
    if (!ready()) { if (++tries < 200) return later(50, wait); }
    if (!ready()) { R.fail.push('地图未初始化'); return done(); }
    later(120, step1);
  })();

  function step1(){
    R.info.cells = CELLS.length; R.info.towns = TOWNS.length;
    R.info.districts = DISTRICTS.features.length;
    R.info.metro = Object.keys(METRO).length;
    R.info.rings = Object.keys(RINGS).length;
    R.info.boards = CELLS.filter(c => !c.est).length;

    ok('默认是板块级视图', curView === 'cell');
    ok('板块图层在图上', map.hasLayer(cellFillLayer) && map.hasLayer(cellLblLayer));
    ok('板块单元全部绘制', cellFillLayer.getLayers().length === CELLS.length,
       [cellFillLayer.getLayers().length, CELLS.length]);
    ok('街镇单元全部绘制', townFillLayer.getLayers().length === TOWNS.length,
       [townFillLayer.getLayers().length, TOWNS.length]);
    ok('街镇数 217', TOWNS.length === 217, TOWNS.length);
    ok('区界 16 个', DISTRICTS.features.length === 16, DISTRICTS.features.length);
    ok('地铁 18 条', Object.keys(METRO).length === 18, Object.keys(METRO).length);
    ok('环线 4 类', Object.keys(RINGS).length === 4, Object.keys(RINGS).length);
    ok('无 JS 报错', __err.length === 0, __err);
    later(60, step2);
  }

  function step2(){   // 视图切换
    document.querySelectorAll('#viewToggle .view-btn')[1].click();
    ok('切到街镇级', curView === 'town');
    ok('街镇图层上位', map.hasLayer(townFillLayer) && !map.hasLayer(cellFillLayer));
    ok('切换后标签层正确', activeLblMarkers === townLblMarkers);
    document.querySelectorAll('#viewToggle .view-btn')[0].click();
    ok('切回板块级', curView === 'cell' && map.hasLayer(cellFillLayer));
    later(60, step3);
  }

  function step3(){   // 图层开关
    const on = id => document.getElementById(id);
    const flip = (id, layer, name) => {
      on(id).checked = false; on(id).dispatchEvent(new Event('change'));
      const offOk = !map.hasLayer(layer);
      on(id).checked = true; on(id).dispatchEvent(new Event('change'));
      ok(name + ' 开关生效', offOk && map.hasLayer(layer), [offOk, map.hasLayer(layer)]);
    };
    flip('ckFill', cellFillLayer, '区域填色');
    flip('ckRings', ringLayer, '环线');
    flip('ckMetro', metroLayer, '地铁');
    flip('ckMetroPlanned', metroPlannedLayer, '在建地铁');
    flip('ckTown', townLineLayer, '街镇界');
    later(60, step4);
  }

  function step4(){   // 标签阈值
    map.setZoom(10.5, {animate:false});
    later(300, () => {
      const few = activeLblLayer.getLayers().length;
      map.setZoom(13, {animate:false});
      later(400, () => {
        const many = activeLblLayer.getLayers().length;
        ok('缩放越大标签越多', many > few, [few, many]);
        ok('低缩放下标签稀疏（不是全部铺满）', few < CELLS.length, [few, CELLS.length]);
        const ck = document.getElementById('ckLbl');
        ck.checked = false; ck.dispatchEvent(new Event('change'));
        const none = activeLblLayer.getLayers().length;
        ck.checked = true; ck.dispatchEvent(new Event('change'));
        ok('关掉名称标签后标签清空', none === 0, none);
        ok('重新打开后标签回来', activeLblLayer.getLayers().length === many);
        later(60, step5);
      });
    });
  }

  function step5(){   // 价位筛选
    const mn = document.getElementById('pfMin'), mx = document.getElementById('pfMax');
    mn.value = 5; mx.value = 7; mn.dispatchEvent(new Event('input'));
    const ps = curView === 'cell' ? cellPolys : townPolys;
    const inRange = ps.filter(p => !p._out).length;
    const consistent = ps.every(p => p._out === !(p._lo != null && p._hi >= 5 && p._lo <= 7));
    ok('价位筛选变灰逻辑自洽', consistent);
    ok('筛选计数与变灰数吻合',
       document.getElementById('pfCount').textContent.includes(`${inRange} / ${ps.length}`),
       document.getElementById('pfCount').textContent);
    ok('筛选后确实有变灰的板块', inRange > 0 && inRange < ps.length, [inRange, ps.length]);
    document.getElementById('pfClear').click();
    ok('清除筛选后全部恢复', ps.every(p => !p._out) && mn.value === '' && mx.value === '');
    document.querySelector('#pfChips button[data-r="7,10"]').click();
    ok('快捷区间按钮生效', mn.value === '7' && mx.value === '10',
       [mn.value, mx.value]);
    document.getElementById('pfClear').click();
    later(60, step6);
  }

  function step6(){   // 弹窗
    const cell = n => cellPolys[CELLS.findIndex(c => c.name === n)];
    const town = n => townPolys[TOWNS.findIndex(t => t.fullname === n)];
    const has = (p, s) => p.getPopup().getContent().includes(s);

    ok('板块弹窗有标题和价格',
       has(cell('静安寺'), '静安寺') && has(cell('静安寺'), '万/㎡'));
    ok('街镇弹窗有全名', has(town('陆家嘴街道'), '陆家嘴街道'));
    ok('环线弹窗可打开', (() => {
         let found = false;
         ringLayer.eachLayer(l => { if (l.getPopup && l.getPopup()) found = true; });
         return found;
       })());
    ok('地铁弹窗可打开', (() => {
         let n = 0;
         metroLayer.eachLayer(l => { if (l.getPopup && l.getPopup()) n++; });
         return n > 0;
       })());

    if (typeof SCHOOLS === 'undefined' || !Object.keys(SCHOOLS).length) {
      R.info.school = 'absent';
      return later(60, step7);
    }
    // —— 学区部分 ——
    R.info.schoolTowns = Object.keys(SCHOOLS).length;
    R.info.schoolMeta = [SCHOOLMETA.year, SCHOOLMETA.schools, SCHOOLMETA.areas];
    const pudong = CELLS.filter(c => c.dist === '浦东');
    ok('浦东板块都能关联到街镇学区数据',
       pudong.every(c => c.tw && SCHOOLS[c.tw]), pudong.filter(c => !(c.tw && SCHOOLS[c.tw])).map(c => c.name));
    ok('非浦东板块不带学区数据',
       CELLS.filter(c => c.dist !== '浦东').every(c => !SCHOOLS[c.tw]));
    ok('浦东板块弹窗显示对口初中', has(cell('联洋'), '对口初中') && has(cell('联洋'), '花木街道'));
    ok('非浦东板块弹窗不显示学区', !has(cell('静安寺'), 'pp-sec'));
    ok('街镇弹窗显示对口初中', has(town('周浦镇'), '对口初中'));
    const xh = TOWNS.find(t => t.dist === '徐汇');
    ok('非浦东街镇弹窗不显示学区', xh != null && !has(town(xh.fullname), 'pp-sec'));
    ok('学区区块含年份与公示字样',
       has(town('周浦镇'), String(SCHOOLMETA.year)) && has(town('周浦镇'), '公示'));

    // 折叠列表：默认收起 + 能展开
    // 街镇弹窗只有在街镇图层挂到地图上时才能打开
    document.querySelectorAll('#viewToggle .view-btn')[1].click();
    const target = town('周浦镇');
    map.setView(target.getBounds().getCenter(), 12, {animate:false});
    target.openPopup();
    later(500, () => {
      const details = document.querySelectorAll('.leaflet-popup-content .pp-sc details');
      ok('每所学校一条折叠项',
         details.length === SCHOOLS['周浦镇'].schools.length, details.length);
      ok('折叠项默认收起', Array.from(details).every(d => !d.open));
      const wrapper = document.querySelector('.leaflet-popup-content-wrapper');
      if (!wrapper) { R.fail.push('弹窗未渲染'); return done(); }
      const h0 = wrapper.offsetHeight;
      details[0].open = true;
      later(350, () => {
        const w1 = document.querySelector('.leaflet-popup-content-wrapper');
        if (!w1) { R.fail.push('展开后弹窗丢失'); return done(); }
        const h1 = w1.offsetHeight;
        ok('展开后弹窗变高', h1 > h0, [h0, h1]);
        const box = document.querySelector('.pp-sc');
        ok('地段列表限高可滚动',
           box.offsetHeight <= 260 && box.scrollHeight > box.clientHeight,
           [box.offsetHeight, box.scrollHeight]);
        ok('弹窗不超出视口', h1 < innerHeight - 20, [h1, innerHeight]);
        ok('地段条目带备注标记（备注存在时）',
           document.querySelectorAll('.pp-area.n').length > 0 ||
           !SCHOOLS['周浦镇'].schools.some(s => s.areas.some(a => a.note)));
        ok('无 JS 报错（含弹窗交互）', __err.length === 0, __err);
        later(60, step7);
      });
    });
  }

  function step7(){   // 图例 / 页脚
    ok('区图例 16 个', document.querySelectorAll('#distLegend .row').length === 16,
       document.querySelectorAll('#distLegend .row').length);
    ok('学区图例已移除', document.getElementById('scLegend') === null);
    if (typeof SCHOOLS !== 'undefined' && Object.keys(SCHOOLS).length) {
      const sf = document.getElementById('scFooter');
      ok('页脚标注了学区数据范围', /36 个街镇/.test(sf.textContent), sf.textContent);
      ok('页脚标注了学区数据来源', /教育局/.test(sf.textContent));
    }
    ok('版本徽标已渲染', /v\d/.test(document.getElementById('verBadge').textContent),
       document.getElementById('verBadge').textContent);
    const lb = document.getElementById('layers').getBoundingClientRect();
    const lt = document.getElementById('legend').getBoundingClientRect();
    ok('桌面端图例不遮挡图层面板', lb.bottom <= lt.top, [Math.round(lb.bottom), Math.round(lt.top)]);
    const rc = document.getElementById('rightCol');
    const lg = document.getElementById('legend');
    ok('图例挂进 rightCol（窗口再小也不会与图层面板重叠）',
       lg.parentElement === rc && getComputedStyle(rc).display === 'flex',
       [lg.parentElement && lg.parentElement.id, getComputedStyle(rc).display]);
    later(60, step8);
  }

  function step8(){   // 新房楼盘：默认关闭 + 开关 + 价位筛选联动
    document.querySelectorAll('#viewToggle .view-btn')[0].click();   // 回到板块级（弹窗统计要用 cellPolys）
    ok('新房测试前回到板块级', curView === 'cell');
    const ck = document.getElementById('ckNew');
    const n = (typeof NEWHOMES === 'undefined') ? 0 : NEWHOMES.length;
    R.info.newhomes = n;
    if (!n) {
      ok('无新房数据时隐藏图层开关',
         document.getElementById('lbNew').style.display === 'none' &&
         document.getElementById('nhLegend').style.display === 'none');
      ok('无 JS 报错（无新房数据）', __err.length === 0, __err);
      return later(60, done);
    }
    ok('新房图层默认关闭', !map.hasLayer(newHomeLayer) && !ck.checked);
    const cp = cellPolys[0];
    ok('勾选新房前区域填色不透明度 .5', cp.options.fillOpacity === .5, cp.options.fillOpacity);
    ck.checked = true; ck.dispatchEvent(new Event('change'));
    ok('勾选新房后区域填色调淡为 .28', cp.options.fillOpacity === .28, cp.options.fillOpacity);
    ok('勾选后新房点全部上图', newHomeLayer.getLayers().length === n,
       [newHomeLayer.getLayers().length, n]);
    ok('新房点按单价着色', nhPoints.every(o => o.mk.options.fillColor === nhColor(o.h.unit_price)));

    const mn = document.getElementById('pfMin'), mx = document.getElementById('pfMax');
    mn.value = 5; mx.value = 7; mn.dispatchEvent(new Event('input'));
    const inRange = h => h.unit_price != null && h.unit_price / 10000 >= 5 && h.unit_price / 10000 <= 7;
    const expectOut = NEWHOMES.filter(h => !inRange(h)).length;
    const gray = nhPoints.filter(o => o.mk.options.fillColor === '#cbd5e1').length;
    ok('价位筛选后新房灰点数正确（无单价或区间外）', gray === expectOut, [gray, expectOut]);
    const passPts = nhPoints.filter(o => !o.mk._out);
    ok('符合点保留档位色',
       passPts.length > 0 && passPts.every(o => o.mk.options.fillColor === nhColor(o.h.unit_price)),
       passPts.length ? passPts.map(o => [o.mk.options.fillColor, nhColor(o.h.unit_price)]).slice(0, 3) : '无符合点');
    const pair = (() => {
      for (const a of [false, true]) {
        const p = passPts.find(o => o.h.approx === a);
        const q = nhPoints.find(o => o.mk._out && o.h.approx === a);
        if (p && q) return [p, q];
      }
      return null;
    })();
    ok('符合点半径大于灰点半径（同 zoom、同 approx）',
       !!pair && pair[0].mk.getRadius() > pair[1].mk.getRadius(),
       pair ? [pair[0].mk.getRadius(), pair[1].mk.getRadius()] : '无同 approx 的符合/灰点可比');
    ok('筛选计数追加新房', /新房 \d+ \/ \d+ 个/.test(document.getElementById('pfCount').textContent),
       document.getElementById('pfCount').textContent);
    document.getElementById('pfClear').click();
    ok('清除筛选后新房无灰点', nhPoints.every(o => !o.mk._out && o.mk.options.fillColor !== '#cbd5e1'));
    map.closePopup();
    later(120, step9);
  }

  function step9(){   // 图层打开时：板块弹窗末尾追加本区域新房统计
    const i = NEWHOMES.findIndex(h => h.cell != null);
    ok('有新房落入板块单元', i >= 0, i);
    if (i < 0) return later(60, step10);
    R.__nhCell = NEWHOMES[i].cell;
    cellPolys[R.__nhCell].openPopup();
    later(220, () => {
      const box = document.querySelector('.leaflet-popup-content');
      const el = box && box.querySelector('.pp-nh');
      ok('板块弹窗末尾追加新房统计', !!el, box ? box.textContent.slice(-70) : '弹窗未渲染');
      ok('统计文字含本板块与数量', !!el && /本板块新房 \d+ 个/.test(el.textContent), el && el.textContent);
      cellPolys[R.__nhCell].closePopup();
      later(120, step9b);   // 图层仍开着，接着做真实点击测试
    });
  }

  function step9b(){   // 真实点击：新房圆点开楼盘弹窗；板块空白处仍开板块弹窗
    const finish = () => {
      const ck = document.getElementById('ckNew');
      ck.checked = false; ck.dispatchEvent(new Event('change'));
      ok('取消勾选后图层移除', !map.hasLayer(newHomeLayer));
      ok('取消新房后区域填色恢复 .5', cellPolys[0].options.fillOpacity === .5,
         cellPolys[0].options.fillOpacity);
      later(120, step10);
    };
    const o = nhPoints.find(p => p.h.cell != null);
    ok('存在可点击的新房圆点', !!o);
    if (!o) { finish(); return; }
    const h = o.h, lat = h.lat, lng = h.lng;
    map.closePopup();
    map.setView([lat, lng], 15, {animate:false});
    later(400, () => {
      const pt = map.latLngToContainerPoint([lat, lng]);
      const rb = document.getElementById('map').getBoundingClientRect();
      const x = rb.left + pt.x, y = rb.top + pt.y;
      const el = document.elementFromPoint(x, y);
      const pane = el && el.closest('.leaflet-pane');
      ok('圆点命中的元素在新房 pane 内',
         !!pane && pane.classList.contains('leaflet-nh-pane'),
         el ? (el.tagName + '.' + (el.getAttribute && el.getAttribute('class') || '')) : 'null');
      if (!el) { R.fail.push('圆点处 elementFromPoint 为空'); finish(); return; }
      el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x, clientY:y}));
      later(220, () => {
        const box = document.querySelector('.leaflet-popup-content');
        const txt = box ? box.textContent : '';
        ok('点击圆点打开楼盘弹窗（含楼盘名与价格）',
           txt.indexOf(h.name) >= 0 && (txt.indexOf('万/㎡') >= 0 || txt.indexOf('价格待定') >= 0),
           txt.slice(0, 80));
        ok('楼盘弹窗不含板块的参考挂牌价', txt.indexOf('参考挂牌价') < 0);
        map.closePopup();
        // 另找一个没有新房、且中心远离任何楼盘圆点的板块作为空白处
        let bi = -1;
        for (let k = 0; k < CELLS.length; k++) {
          const c = CELLS[k].c;
          if (NEWHOMES.some(x => x.cell === k)) continue;
          if (nhPoints.some(p => Math.hypot(p.h.lat - c[0], p.h.lng - c[1]) <= 0.02)) continue;
          bi = k; break;
        }
        ok('存在无新房覆盖的板块空白处', bi >= 0, bi);
        if (bi < 0) { finish(); return; }
        const c = CELLS[bi].c;
        map.setView(c, 15, {animate:false});
        later(400, () => {
          const pt2 = map.latLngToContainerPoint(c);
          const x2 = rb.left + pt2.x, y2 = rb.top + pt2.y;
          const el2 = document.elementFromPoint(x2, y2);
          const pane2 = el2 && el2.closest('.leaflet-pane');
          ok('空白处命中的元素不在新房 pane 内',
             !(pane2 && pane2.classList.contains('leaflet-nh-pane')),
             el2 ? (el2.tagName + '.' + (el2.getAttribute && el2.getAttribute('class') || '')) : 'null');
          if (!el2) { R.fail.push('空白处 elementFromPoint 为空'); finish(); return; }
          el2.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x2, clientY:y2}));
          later(220, () => {
            const box2 = document.querySelector('.leaflet-popup-content');
            const txt2 = box2 ? box2.textContent : '';
            ok('空白处点击弹出板块弹窗（含参考挂牌价）', txt2.indexOf('参考挂牌价') >= 0, txt2.slice(0, 80));
            map.closePopup();
            finish();
          });
        });
      });
    });
  }

  function step10(){   // 图层关闭时不追加统计；楼盘弹窗内容
    const ck = document.getElementById('ckNew'), i = R.__nhCell;
    if (i != null) {
      cellPolys[i].openPopup();
      const box = document.querySelector('.leaflet-popup-content');
      ok('图层关闭时板块弹窗不含新房统计', !(box && box.querySelector('.pp-nh')),
         box ? box.textContent.slice(-60) : '弹窗未渲染');
      cellPolys[i].closePopup();
    }
    if (!nhPoints.length) {
      ok('无 JS 报错（含新房图层）', __err.length === 0, __err);
      return later(60, done);
    }
    ck.checked = true; ck.dispatchEvent(new Event('change'));
    const o = nhPoints[0];
    o.mk.openPopup();
    later(220, () => {
      const box = document.querySelector('.leaflet-popup-content');
      const txt = box ? box.textContent : '';
      ok('楼盘弹窗含楼盘名', txt.indexOf(o.h.name) >= 0, txt.slice(0, 60));
      ok('楼盘弹窗带房天下来源链接',
         txt.indexOf('房天下') >= 0 && !!(box && box.querySelector('a[href*="fang.com"]')));
      o.mk.closePopup();
      // approx（坐标只精确到道路）的楼盘：弹窗要有位置说明，圆点半径比精确点小 1
      const ap = nhPoints.find(p => p.h.approx);
      if (!ap) {
        ok('approx 楼盘弹窗含「道路级估计」（跳过：本次数据无 approx 楼盘）', true);
        ck.checked = false; ck.dispatchEvent(new Event('change'));
        ok('无 JS 报错（含新房图层）', __err.length === 0, __err);
        return later(60, done);
      }
      ap.mk.openPopup();
      later(220, () => {
        const b2 = document.querySelector('.leaflet-popup-content');
        const t2 = b2 ? b2.textContent : '';
        ok('approx 楼盘弹窗含「道路级估计」', t2.indexOf('道路级估计') >= 0, t2.slice(0, 80));
        const exact = nhPoints.find(p => !p.h.approx);
        ok('approx 点半径比精确点小 1',
           !exact || ap.mk.getRadius() === exact.mk.getRadius() - 1,
           exact ? [ap.mk.getRadius(), exact.mk.getRadius()] : '无精确点可比');
        ap.mk.closePopup();
        ck.checked = false; ck.dispatchEvent(new Event('change'));
        ok('无 JS 报错（含新房图层）', __err.length === 0, __err);
        later(60, done);
      });
    });
  }

  function done(){
    if (R.__done) return;
    R.__done = true;
    R.info.errors = __err;
    document.body.setAttribute('data-out', JSON.stringify(R));
  }
})();
</script>
"""


def browser_run(html_text, label):
    c = chrome()
    if not c:
        skip(f"浏览器行为（{label}）", "找不到 Chrome")
        return None
    tmp = tempfile.mkdtemp(prefix="reg-")
    page = os.path.join(tmp, "page.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html_text.replace("</body>", BROWSER_JS + "</body>"))
    try:
        r = sh([c, "--headless", "--disable-gpu", "--hide-scrollbars",
                "--window-size=1500,1000", "--virtual-time-budget=20000",
                "--dump-dom", page])
        m = re.search(r'data-out="(.*?)"', r.stdout, re.S)
        if not m:
            return {"fail": [f"浏览器未返回结果（{label}）"], "pass": [], "info": {}}
        raw = m.group(1)
        for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'")):
            raw = raw.replace(a, b)
        return json.loads(raw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_browser(html_now):
    section("E. 浏览器行为（无头 Chrome）")
    res = browser_run(html_now, "current")
    if res is None:
        return
    for p in res.get("pass", []):
        PASS.append("浏览器：" + p)
    for f in res.get("fail", []):
        FAIL.append("浏览器：" + f)
    info = res.get("info", {})
    print("   " + " · ".join(f"{k}={v}" for k, v in info.items() if k != "errors"))
    if info.get("errors"):
        print(f"   页面内错误：{info['errors']}")


def main():
    no_base = "--no-base" in sys.argv
    if not os.path.exists(os.path.join(ROOT, "boards.py")):
        sys.exit("看起来不在项目根目录内运行")

    html_now = test_build()
    if html_now is None:
        report()
        return
    if not no_base:
        test_baseline(html_now)
    else:
        skip("基线对比", "--no-base")
    test_aggregate()
    test_no_school_file()
    test_no_newhome_file()
    test_browser(html_now)
    report()


def report():
    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 66)
    if "-v" in sys.argv:
        for p in PASS:
            print(f"✓ {p}")
    if SKIP:
        for s in SKIP:
            print(f"⊘ {s}")
    for f in FAIL:
        print(f"✗ {f}")
    print(f"通过 {len(PASS)} / {total}" + (f"，跳过 {len(SKIP)}" if SKIP else "")
          + ("  ✅ 全部通过" if not FAIL else f"  ❌ {len(FAIL)} 项失败"))
    print("=" * 66)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
