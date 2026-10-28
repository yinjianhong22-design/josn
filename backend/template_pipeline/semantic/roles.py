# -*- coding: utf-8 -*-

"""用版式与邻接关系为格子标注语义角色，并标记可直接填写的空格。

角色含义：title 是表单标题，header 是表体列名，label 是待填字段的名称，
value 是空白填写格，data 是其余已有内容。角色描述的是格子在表单中的用途，
不等于数据简表的 specified/measured/pass_rate 列角色。

识别依赖字体、边框、表头与表体的位置以及合并格邻接关系；字段配对阶段还会
把带示例文本的值格补标为 fillable，因此本阶段的 value 并非全部可写值格。
"""

from importlib import import_module as _import_module

from ..constants import _NO_LABEL_PREFIX
from .fields import _is_placeholder

_xlsx_anchor = _import_module('..align.xlsx_anchor', __package__)
_ensure_pseudo_addr = _xlsx_anchor._ensure_pseudo_addr
from .table_topology import _pick_header_row, _table_rows_set


def assign_roles(data):
    """给格子标注 title/header/label/value/data，并按需标记 fillable。

    这是供回填定位使用的有序启发式流程：先识别标题和真表头，再由表头向下
    圈定表体，随后处理表体空格、表单体标签及其相邻值格，最后把剩余非空格
    归为 data。表头以下的空边框格均可填，包括专用填写行和备用空行；表尾
    的“检查结论/审核人”等区域不参与表体规则。

    结果会直接写回各 cell，并打印角色统计，可配合 overlay.png 人工核对。
    """
    _ensure_pseudo_addr(data)
    cells = data['cells']
    if not cells:
        return
    import re
    by_grid = {(c['row'], c['col']): c for c in cells}
    page_cx = data['width'] / 2

    def text_of(c):
        return (c.get('text') or '').replace('\n', '').strip()

    def cell_sizes(c):
        return [l['size'] for l in c.get('lines', []) if 'size' in l]

    sizes = sorted(s for c in cells for s in cell_sizes(c))
    # 用全页字号中位数代表正文，避免少量标题大字把标题识别阈值整体抬高。
    body = sizes[len(sizes) // 2] if sizes else 10.0

    # 1) title: 格内大字且接近页水平居中 (四边全框的格是表格字段, 排除)
    for c in cells:
        b = c.get('borders') or {}
        if all(b.get(s) for s in ('top', 'bottom', 'left', 'right')):
            continue
        szs = cell_sizes(c)
        # 标题阈值：最大字号至少为正文 1.3 倍，水平偏移小于页宽 12%
        if szs and max(szs) >= body * 1.3 \
                and abs(c['x'] + c['w'] / 2 - page_cx) < data['width'] * 0.12:
            c['role'] = 'title'

    # 2) header: 某行非空格 >=3、全部短文本, 且 >=2 个表头列的下一行同列
    #    有内容 (真表头直接压在数据列上; "抽检人/抽检时间/年月日"这类
    #    信息行下面是空行或另一表头, 不会误标)
    header_rows = set()
    by_row = {}
    for c in cells:
        by_row.setdefault(c['row'], []).append(c)
    for r, row_cells in sorted(by_row.items()):
        non_empty = [text_of(c) for c in row_cells if text_of(c)]
        if len(non_empty) < 3 or not all(len(t) <= 10 for t in non_empty):
            continue
        # 真表头是文字列名; 纯数字/符号的短文本是数据 (已填表照片的
        # 实测值行也满足 ">=3 短文本 + 下排有内容", 必须靠中文字
        # 占比把它们挡在表头外)
        cjk = sum(1 for t in non_empty
                  if re.search(r'[一-鿿]', t))
        if cjk < 2 or cjk * 2 < len(non_empty):
            continue
        hcols = {c['col'] for c in row_cells if text_of(c)}
        filled_below = sum(
            1 for hc in hcols
            if (nxt := by_grid.get((r + 1, hc))) is not None
            and text_of(nxt))
        if filled_below < 2:
            # 空白模板兜底: 表头下一行该列是带框空格 (整排待填, 如
            # 规定值/实测值/合格率全空的数据行) 同样算表头
            empty_grid = sum(
                1 for hc in hcols
                if (nxt := by_grid.get((r + 1, hc))) is not None
                and not text_of(nxt)
                and any((nxt.get('borders') or {}).values()))
            if empty_grid < 2:
                continue
        for c in row_cells:
            if text_of(c):
                c['role'] = 'header'
        header_rows.add(r)

    header_row = _pick_header_row(cells, header_rows)

    # 2.5) 表体区 (检查结论/审核人等表单尾区不在其中)
    table_rows = _table_rows_set(cells, header_row) \
        if header_row is not None else set()

    # 3) 表体区的空边框格: 可填 (含专用填写行/备用空行)
    for c in cells:
        if c['row'] in table_rows and not text_of(c) \
                and any((c.get('borders') or {}).values()):
            c['role'] = 'value'
            c['fillable'] = True

    # 4) label: 非空短文本, 不以数值/符号开头, 不在表体区,
    #    右邻为空(或下划线占位)或下邻为空
    for c in cells:
        if c.get('role') or c['row'] in table_rows:
            continue
        t = text_of(c)
        if not t or len(t) > 12 or t[0] in _NO_LABEL_PREFIX:
            continue
        # 邻居查找跨过当前格的 colspan/rowspan，适配合并标签格
        right = by_grid.get((c['row'], c['col'] + c['colspan']))
        below = by_grid.get((c['row'] + c['rowspan'], c['col']))
        # 下划线和日期占位虽非空，仍按可填写值格处理
        right_empty = right is not None and (
            not text_of(right) or '___' in (right.get('text') or '')
            or _is_placeholder(right.get('text')))
        below_empty = below is not None and not text_of(below)
        if right_empty or below_empty:
            c['role'] = 'label'

    # 5) value: 空格, 左邻或上邻是 label (表体区已在 3 覆盖)
    for c in cells:
        if c.get('role') or text_of(c):
            continue
        left = by_grid.get((c['row'], c['col'] - 1))
        above = by_grid.get((c['row'] - 1, c['col']))
        if (left is not None and left.get('role') == 'label') or \
           (above is not None and above.get('role') == 'label'):
            c['role'] = 'value'
            c['fillable'] = True

    # 6) data: 其余非空格
    for c in cells:
        if not c.get('role') and text_of(c):
            c['role'] = 'data'

    roles = {}
    for c in cells:
        r = c.get('role') or '-'
        roles[r] = roles.get(r, 0) + 1
    print(f'角色标注: {dict(sorted(roles.items()))}')
