# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# 数据简表: 一次性匹配 JSON (用完即弃, 不落盘)
#
# 数据侧只回答两个问题: 这张表是什么 (title 路由模板), 有哪些字段和值
# (name 匹配模板索引; specified/measured[]/pass_rate 是要填的值)。
# 坐标/边框/字体一概不需要 —— 值该进哪几个格子由模板自己决定
# (index rows[].columns[列名].cells: 实测值列区子格按 x 序的 uuid 清单,
# measured 按序拆格; 规定值/合格率列保持单格)。
# 已填表照片/扫描件经识别和按行标签取值后走 match_data_fields。
# ---------------------------------------------------------------------------

from .column_roles import normalize_column_role


def photo_to_sheet(data):
    """将已填表照片的识别结果压缩为数据简表 {title, context, fields}。

    输入是 extract_page + assign_roles/extract_fields/build_index 的产物：
    先提取标题供模板路由，再汇总表头/表尾上下文，最后按统一列角色读取
    各数据行的规定值、实测值和合格率。坐标、边框、字体等几何与样式信息
    到此全部舍弃，目标格的位置由后续模板索引决定。

    输出供 match_data_fields 消费，不落盘。拍照件透视矫正是独立前置步骤，
    这里假定输入为正面、清晰的页面图像。"""
    import re

    def norm(s):
        return re.sub(r'\s+', '', str(s or ''))

    title_kw = ('记录表', '检验表', '检测报告', '评定表', '验收表', '检查表')
    ctx_kw = ('单位', '合同段', '人', '抽检', '时间', '部位', '编号', '桩号', '日期')
    texts = sorted(data.get('texts') or [],
                   key=lambda t: (t.get('y') or 0, t.get('x') or 0))
    # 1) 标题：优先从独立文本中选取完整表名，供后续模板路由。
    title = None
    for t in texts:
        s = norm(t.get('text'))
        # 过滤过短文本的误命中；多个候选同时存在时保留更完整的表名。
        if len(s) >= 6 and any(k in s for k in title_kw):
            if title is None or len(s) > len(title):
                title = s
    if title is None:
        # 标题与项目名称、编号圈在同一格时逐行查找，避免把上下行粘进表名。
        cand = []
        for c in data.get('cells') or []:
            for line in str(c.get('text') or '').splitlines():
                s = norm(line)
                if len(s) >= 6 and any(k in s for k in title_kw):
                    ends = [s.find(k) + len(k) for k in title_kw if k in s]
                    # 截到最早结束的关键词，去掉表名后串接的编号等内容。
                    cand.append((c.get('y') or 0, s[:min(ends)]))
        if cand:
            cand.sort(key=lambda p: p[0])
            title = cand[0][1]

    idx = data.get('index') or {}
    cells = idx.get('cells') or {}

    def cell_text(u):
        return ((cells.get(u) or {}).get('text') or '').strip()

    def ctx_value(name, t):
        """整行共享格 "监理单位：… 编 号：JB12-x" -> 冒号后值段。"""
        m = re.match(r'^(.*?)[:：]\s*(\S.*)$', t or '')
        if m and norm(name) in norm(m.group(1)):
            return m.group(2).strip()
        return t

    # 2) 上下文：优先使用已建立的标签->值格索引，再从原始格文本补漏。
    context = {}
    for name, ent in (idx.get('header_fields') or {}).items():
        if not any(k in name for k in ctx_kw):
            continue
        u = ent.get('cell') if isinstance(ent, dict) else ent
        t = cell_text(u)
        if t:
            context[name] = ctx_value(name, t)
    for name, ents in (idx.get('by_label') or {}).items():
        if name in context or not any(k in name for k in ctx_kw):
            continue
        u = next((e.get('value') for e in ents if e.get('value')), None)
        t = cell_text(u) if u else ''
        if t:
            context[name] = ctx_value(name, t)

    def add_context(name, value):
        """补充索引漏掉的已填写上下文；已有索引值优先。"""
        name = norm(name).rstrip(':：')
        value = str(value or '').strip()
        # 上下文名称限定为短标签，避免把整句识别文本误作字段名。
        if not name or len(name) > 12 or not value \
                or not any(k in name for k in ctx_kw):
            return
        if any(norm(old) == name for old in context):
            return
        context[name] = value

    # 标题格内的 "编号：..." 等短行不一定进入字段索引，逐行补充。
    for c in data.get('cells') or []:
        for line in str(c.get('text') or '').splitlines():
            m = re.match(r'^\s*(.{1,12}?)\s*[:：]\s*(\S.*?)\s*$', line)
            if m:
                add_context(m.group(1), m.group(2))

    # 已填表的标签右格本来就有值，不能只依赖面向空模板的字段配对。
    by_grid = {(c['row'], c['col']): c for c in data.get('cells') or []}
    for c in data.get('cells') or []:
        # 列坐标加上 colspan，才能定位合并标签格右侧的相邻值格。
        target = by_grid.get((c['row'], c['col'] + c.get('colspan', 1)))
        if target is not None:
            add_context(c.get('text'), target.get('text'))

    # 3) 数据行：列角色屏蔽模板列名差异；实测子格按索引中的顺序读取，
    # 输出只保留业务字段及其值，不带任何几何信息。
    fields = []
    for r in idx.get('rows') or []:
        f = {'name': (r.get('key') or '').strip()}
        if not f['name']:
            continue
        got = False
        for hn, col in (r.get('columns') or {}).items():
            role = normalize_column_role(hn)
            if role == 'specified':
                t = cell_text(col['cell'])
                if t:
                    f['specified'] = t
                    got = True
            elif role == 'measured':
                vals = []
                for u in (col.get('cells') or [col['cell']]):
                    vals.extend(_split_numbers(cell_text(u)))
                if vals:
                    f['measured'] = vals
                    got = True
            elif role == 'pass_rate':
                # 合格率按数值输出（如 95% -> 95），无法转换时忽略该列。
                t = cell_text(col['cell']).replace('%', '').strip()
                try:
                    f['pass_rate'] = float(t) if '.' in t else int(t)
                    got = True
                except ValueError:
                    pass
        # 合格率不能独立构成有效数据行；至少有规定值或实测值才输出，
        # 防止空模板行或仅含统计值的行进入后续字段匹配。
        if got and (f.get('measured') or f.get('specified')):
            fields.append(f)
    return {'title': title, 'context': context, 'fields': fields}


def _split_numbers(s):
    """将单元格文本严格解析为数值列表，无法完整解析时返回 []。

    支持中英文逗号、顿号、分号和空白分隔。任一段含非数值即整格放弃，
    避免把“数值 + 备注”等混合文本误当成部分实测值。手写数字 OCR 常把
    小数点读成横线（37-6 -> 37.6），数字间的 -/—/– 按小数点容错。
    """
    import re
    # OCR 可能在小数点两侧插入空格，先合并再按分隔符拆分数值。
    text = re.sub(r'(?<=\d)\s*\.\s*(?=\d)', '.', str(s or ''))
    parts = [p.strip() for p in re.split(r'[，,、;；\s]+', text)
             if p.strip()]
    out = []
    for p in parts:
        p = re.sub(r'(?<=\d)[-—–](?=\d)', '.', p.replace('%', ''))
        try:
            out.append(float(p) if '.' in p else int(p))
        except ValueError:
            return []
    return out
