# -*- coding: utf-8 -*-

"""把字段配对和表格拓扑编译为带诊断信息的可填充索引。

索引以 UUID 为主键，包含五部分：
- ``header_fields``：表头以上的上下文字段，按规范化字段名直接查找；
- ``rows``：表体行，按“行标签 -> 列名 -> 值格”还原规定值、实测值等结构；
- ``by_label``：全表标签兜底索引，一个规范化标签可对应多个格子；
- ``cells``：UUID 到地址、几何、文本、字体及填充语义的映射；
- ``report``：构建期发现的降级、缺失、重名和占位填充等诊断。

有原生地址的格子用固定命名空间和 addr 生成 UUID5，因此同表重建结果稳定；
无地址格子只能使用随机 UUID。回填渲染应以 cells 中的 UUID 为定位键，而
不是重复解释字段配对规则。report 用于尽早暴露问题，不会替代可用索引。

cells[uuid].mode 的填充语义：
- overwrite：空格直接写入；
- replace：整格替换示例或占位文本；
- append：在标签文本后追加值；
- inline：只替换标签前缀之后的占位片段。

共享格可能被多个字段引用。顶层 mode/prefix/placeholder 保留旧索引契约，
bindings 则逐字段保存各自的填充方式，防止后登记字段覆盖先登记字段。
"""

from importlib import import_module as _import_module

_xlsx_anchor = _import_module('..align.xlsx_anchor', __package__)
from .table_topology import (
    _group_free_text_lines,
    _label_col,
    _pick_header_row,
    _table_rows_set,
    discover_table,
)


_INDEX_NS = None  # uuid5 namespace, 延迟生成


def build_index(data):
    """生成 data['index']，把字段和表格拓扑解析为 UUID 引用。

    依赖 assign_roles 的角色、extract_fields 的配对结果及网格几何。上下文字段
    与表体行分别进入 header_fields 和 rows，其余标签继续汇入 by_label；字段名
    和行标签的索引键均去除全部空白，但 cells 中保留原始文本供回填定位。

    构建以“尽量保留可用索引”为原则：未识别表头、缺值格、缺列、重名和几何
    降级等问题写入 report，而不是在首个异常处中止整个索引。
    """
    import re
    import uuid
    global _INDEX_NS
    if _INDEX_NS is None:
        _INDEX_NS = uuid.UUID('7d4f2a90-3b1c-5e6a-9f08-2d5b8c4a7e91')

    def norm(s):
        return re.sub(r'\s+', '', str(s or ''))

    cells = data['cells']
    topology = discover_table(cells)
    by_grid = topology['by_grid']
    header_row = topology['header_row']

    # 同 addr 的游离文字按视觉行拼接 (一个原生格常被拆成多个 seg,
    # 如 A3 = "监理单位：..." + "JB12-4.2.2-"), 拼接后占位提取才完整
    merged_text = {}
    for grp in _group_free_text_lines(data.get('texts') or []):
        by_a = {}
        for it in grp['items']:
            t = data['texts'][it['idx']]
            if t.get('addr'):
                by_a.setdefault(t['addr'], []).append(it)
        for a, items in by_a.items():
            merged_text[a] = ''.join(
                data['texts'][it['idx']]['text']
                for it in sorted(items, key=lambda i: i['x']))

    idx_cells, report = {}, []
    # report 是构建期诊断而非异常清单：warn 提示缺失、冲突或降级，
    # info 记录占位填充、实测拆格等自动决策，便于回填前人工复核。
    _noaddr = {}  # id(cell) -> uuid (无 addr 的格, 每次运行不同)

    def ref(c):
        """cell -> uuid, 并登记进 cells 表 (几何/文本/占位)。"""
        a = c.get('addr')
        if a:
            u = str(uuid.uuid5(_INDEX_NS, a))
        else:
            u = _noaddr.setdefault(id(c), str(uuid.uuid4()))
        # 共享格只初始化一次；各字段的填充差异后续写入 bindings
        if u not in idx_cells:
            e = {'addr': a, 'x': round(c['x'], 1), 'y': round(c['y'], 1),
                 'w': round(c.get('w') or 0, 1),
                 'h': round(c.get('h') or 0, 1)}
            t = merged_text.get(a, (c.get('text') or '').strip())
            if t:
                e['text'] = t
            # 字号/字体/颜色: PDF 文字层 (lines, 实际打印样式) 优先,
            # 其次使用格子的顶层样式属性; 供已有模板索引消费。
            ln0 = (c.get('lines') or [{}])[0] or {}
            size = c.get('size') or ln0.get('size') or c.get('font_size')
            bold = (c.get('bold') or ln0.get('bold')
                    or c.get('font_bold'))
            family = (c.get('font') or ln0.get('font')
                      or c.get('font_name'))
            color = (c.get('color') or ln0.get('color')
                     or c.get('font_color'))
            align = c.get('align') or c.get('font_align')
            if size:
                e['size'] = round(float(size), 2)
            if bold:
                e['bold'] = True
            if family:
                e['font'] = str(family)
            if color:
                e['color'] = str(color)
            if align:
                e['align'] = align
            idx_cells[u] = e
        return u

    def mode_of(u, f):
        """登记字段填充方式; 共享格逐字段保留 prefix/placeholder。"""
        if f.get('inline'):
            mode = 'inline'
        elif f.get('replace'):
            mode = 'replace'
        elif f.get('append'):
            mode = 'append'
        else:
            mode = 'overwrite'
        cell = idx_cells[u]
        text = cell.get('text') or ''
        prefix = f.get('prefix')
        if mode == 'append' and prefix:
            placeholder = prefix
        elif mode == 'inline' and text:
            placeholder = None
            if prefix and prefix in text:
                start = text.index(prefix) + len(prefix)
                ends = []
                # 同一原生格可含多个字段, 当前字段的值段到下一字段前缀止。
                for other in data.get('fields') or []:
                    op = other.get('prefix')
                    other_addr = other.get('value') or other.get('label')
                    if other is f or not op or op == prefix \
                            or other_addr != cell.get('addr'):
                        continue
                    pos = text.find(op, start)
                    if pos >= 0:
                        ends.append(pos)
                # 有多个后续字段时，最近的前缀才是当前值段边界
                end = min(ends) if ends else len(text)
                placeholder = text[start:end].strip() or None
            if placeholder is None and not prefix:
                # 缺少前缀时，用最后一个冒号后的内容兜底识别占位
                placeholder = re.split(r'[:：]', text)[-1].strip() or text
        elif text:
            placeholder = text
        else:
            placeholder = None

        # 顶层字段保持旧索引结构; bindings 让共享格的每个字段仍使用自己的
        # 填充语义, 不会被最后登记的字段覆盖。
        cell['mode'] = mode
        if prefix:
            cell['prefix'] = prefix
        else:
            cell.pop('prefix', None)
        if placeholder:
            cell['placeholder'] = placeholder
        else:
            cell.pop('placeholder', None)
        binding = {'name': f['name'], 'mode': mode}
        if prefix:
            binding['prefix'] = prefix
        if placeholder:
            binding['placeholder'] = placeholder
        cell.setdefault('bindings', []).append(binding)
        if placeholder:
            report.append({
                'level': 'info', 'type': 'placeholder_fill',
                'field': f['name'], 'cell': u, 'addr': cell['addr'],
                'placeholder': placeholder, 'mode': mode,
                'message': f'"{f["name"]}" 值格含占位文本 '
                           f'"{placeholder}", 按 {mode} 填充'})
        return mode

    header_fields, by_label, rows = {}, {}, []

    def add_label(c, value_u):
        t = norm((c.get('text') or '').replace('\n', ''))
        if not t:
            return
        ent = by_label.setdefault(t, [])
        u = ref(c)
        if any(e['cell'] == u for e in ent):
            return
        e = {'cell': u}
        if value_u:
            e['value'] = value_u
        ent.append(e)

    if header_row is None:
        report.append({'level': 'warn', 'type': 'no_header',
                       'message': '未识别到表头行, header_fields/rows 为空, '
                                  '仅 by_label 可用'})
    # label 可能是格子也可能是游离文字 (监理单位/编号), 游离文字无
    # 行号, 上下文区用 y 坐标与表头行比较定区
    all_ents = list(cells) + list(data.get('texts') or [])

    def find_ent(addr):
        return next((c for c in all_ents if c.get('addr') == addr), None)

    if header_row is not None:
        # 以表头最上沿为界，只把它上方的字段收入 header_fields
        hdr_y = min(c['y'] for c in cells if c['row'] == header_row)
        # ---- 上下文区: 表头行以上的 fields (label -> 同排右侧值格) ----
        for f in data.get('fields') or []:
            label_ent = find_ent(f.get('label'))
            if label_ent is None or label_ent.get('y', 0) >= hdr_y:
                continue  # 表尾/表内字段 (检查结论/审核日期等) 不进 header_fields
            if not f.get('value') and not (f.get('inline')
                                           and f.get('prefix')):
                report.append({'level': 'warn', 'type': 'no_value_cell',
                               'field': f['name'], 'addr': f.get('label'),
                               'message': f'"{f["name"]}" 未配到值格, '
                                          f'仅进 by_label'})
                add_label(label_ent, None)
                continue
            value_ent = find_ent(f.get('value'))
            if value_ent is None:
                value_ent = label_ent   # 标签与值同格 (内联前缀型), 追加填充
                f = {**f, 'inline': False, 'append': True}
            u = ref(value_ent)
            key = norm(f['name'])

            def _reg_label():
                # 以字段名登记 by_label (label 全文可能含多字段, 如 A3 行)
                ent = by_label.setdefault(key, [])
                if not any(x['cell'] == ref(label_ent) for x in ent):
                    x = {'cell': ref(label_ent)}
                    if u != ref(label_ent):
                        x['value'] = u
                    ent.append(x)

            if key in header_fields:
                report.append({'level': 'warn', 'type': 'dup_field',
                               'field': key,
                               'message': f'上下文区重名字段 "{key}", 后者 '
                                          f'仅进 by_label'})
                _reg_label()
                continue
            header_fields[key] = {'cell': u}
            header_fields[key]['mode'] = mode_of(u, f)
            _reg_label()

        # ---- 数据区: 表体行 -> 字段 -> 各列 (规定值/实测值/合格率) ----
        headers = topology['headers']
        header_cols = {hname: hc for hc, hname in headers.items()}
        for table_row in topology['rows']:
            kc = table_row['key_cell']
            kt = table_row['key']
            row_doc = {'key': kt, 'key_cell': ref(kc)}
            add_label(kc, None)
            for hname, target in table_row['columns'].items():
                if target is None or not target.get('addr'):
                    report.append({'level': 'warn',
                                   'type': 'missing_col',
                                   'row': kt, 'col': hname,
                                   'message': f'行 "{kt}" 缺列 '
                                              f'"{hname}" 的值格'})
                    continue
                col = {'cell': ref(target)}
                if (target.get('text') or '').strip():
                    col['text'] = target['text']
                if '实测' in norm(hname):
                    # 实测值类列: 登记列区内全部子格 (按 行,x 序),
                    # 数据简表 measured 列表按序拆格填入 (模板拆格子)
                    hc = header_cols[hname]
                    hcell = by_grid.get((header_row, hc))
                    r = kc['row']
                    rs = kc.get('rowspan', 1)
                    if hcell is not None and rs:
                        hx0 = hcell['x']
                        hx1 = hcell['x'] + hcell['w']
                        # 子格中心须落在实测表头宽度内，且行号受当前行标签 rowspan 限制
                        subs = [c for c in cells
                                if c is not kc and c.get('addr')
                                and hx0 - 1 <= c['x'] + c['w'] / 2
                                < hx1 + 1
                                and r <= c['row'] < r + rs]
                        # 与 measured 列表的消费顺序一致：先按行，再按 x 从左到右
                        subs.sort(key=lambda c: (c['row'], c['x']))
                        col['cells'] = [ref(c) for c in subs]
                        report.append({
                            'level': 'info', 'type': 'value_cells',
                            'row': kt, 'col': hname,
                            'count': len(col['cells']),
                            'message': f'行 "{kt}" 列 "{hname}" 有 '
                                       f'{len(col["cells"])} 个值格, '
                                       f'measured 按序拆格填入'})
                row_doc.setdefault('columns', {})[hname] = col
            if row_doc.get('columns'):
                rows.append(row_doc)
            else:
                report.append({'level': 'info',
                               'type': 'no_fillable_col',
                               'row': kt,
                               'message': f'行 "{kt}" 没有可填列 '
                                          f'(如检查要求说明行), 不进 rows'})

    # ---- by_label 兜底: 表尾 label 角色格 (检查结论/审核人 等) ----
    registered = {e['cell'] for lst in by_label.values() for e in lst}
    for c in cells:
        if c.get('role') != 'label' or not c.get('addr'):
            continue
        u = ref(c)
        if u in registered:
            continue
        # 跨过标签合并区域找右邻，作为未配对字段的值格候选
        right = by_grid.get((c['row'], c['col'] + c.get('colspan', 1)))
        value_u = None
        if right is not None and right.get('addr'):
            value_u = ref(right)
            if (right.get('text') or '').strip():
                idx_cells[value_u]['mode'] = 'replace'
                idx_cells[value_u]['placeholder'] = \
                    (right.get('text') or '').strip()
                report.append({
                    'level': 'info', 'type': 'placeholder_fill',
                    'field': norm(c.get('text')), 'cell': value_u,
                    'addr': right['addr'],
                    'placeholder': (right.get('text') or '').strip(),
                    'mode': 'replace',
                    'message': f'"{norm(c.get("text"))}" 值格含占位文本, '
                               f'按 replace 填充'})
        add_label(c, value_u)

    # fields 里尚未登记的标签 (审核日期/监理单位/编号 等非 label 角色,
    # label 可能是格子也可能是游离文字)
    for f in data.get('fields') or []:
        label_ent = find_ent(f.get('label'))
        if label_ent is None:
            continue
        u = ref(label_ent)
        if u in {e['cell'] for lst in by_label.values() for e in lst}:
            continue
        value_u = None
        value_ent = find_ent(f.get('value'))
        if value_ent is not None and f.get('value') != f.get('label'):
            value_u = ref(value_ent)
        elif value_ent is not None:
            value_u = u  # label/value 同格 (inline/append)
        # 以字段名 (而非 label 格全文) 登记, 游离文字 label 的全文
        # 可能含多个字段 (如 A3 一整行)
        key = norm(f['name'])
        ent = by_label.setdefault(key, [])
        if not any(x['cell'] == u for x in ent):
            x = {'cell': u}
            if value_u:
                x['value'] = value_u
            ent.append(x)

    # 重名数据行显式报告; 匹配阶段仍会按输入顺序逐行各消费一次。
    duplicate_rows = {}
    for i, row in enumerate(rows):
        duplicate_rows.setdefault(norm(row.get('key')), []).append(i)
    for key, positions in duplicate_rows.items():
        if key and len(positions) > 1:
            report.append({
                'level': 'warn', 'type': 'duplicate_row', 'row': key,
                'count': len(positions), 'rows': positions,
                'message': f'数据区重名行 "{key}" 共 {len(positions)} 行, '
                           f'匹配时按顺序逐行消费'})

    # xlsx 对齐失败或纯图片通路使用几何伪地址, 索引必须保留降级痕迹。
    pseudo_cells = sum(1 for c in cells
                       if str(c.get('addr') or '').startswith('#G'))
    pseudo_texts = sum(1 for t in data.get('texts') or []
                       if str(t.get('addr') or '').startswith('#T'))
    if pseudo_cells or pseudo_texts:
        report.append({
            'level': 'warn', 'type': 'alignment_degraded',
            'cells': pseudo_cells, 'texts': pseudo_texts,
            'message': f'语义对齐降级为几何伪地址: 格子 {pseudo_cells} 个, '
                       f'游离文字 {pseudo_texts} 个'})

    data['index'] = {
        'meta': {'title': (data.get('form') or {}).get('meta', {})
                 .get('title'),
                 'dpi': data.get('dpi'), 'width': data.get('width'),
                 'height': data.get('height'),
                 'header_row': header_row,
                 'norm': '去除所有空白字符'},
        'header_fields': header_fields,
        'rows': rows,
        'by_label': by_label,
        'cells': idx_cells,
        'report': report,
    }
    print(f"index: header_fields {len(header_fields)} 个, rows {len(rows)}"
          f" 行, by_label {len(by_label)} 项, 报告 {len(report)} 条")
