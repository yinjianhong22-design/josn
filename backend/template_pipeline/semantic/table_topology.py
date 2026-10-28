# -*- coding: utf-8 -*-

"""统一发现表头、表体、行标签列和各列候选值格。

角色标注、字段配对和索引构建共用这里的拓扑，避免各自推断表体边界而产生
错位。拓扑描述“哪一行属于哪一列”：headers 保存列号到列名，table_rows 是
连续表体的行号集合，rows 则保存行标签格及每个表头列的目标格。

目标格不等于待填空格：已有内容的规定值格也会保留，供索引展示或校验；是否
可填由 fields/index 分别筛选。找不到合适目标时用 None 保留缺列事实。
"""

from ..constants import _NO_LABEL_PREFIX


def _norm_text(s):
    """去掉所有空白后的文本, 用于跨源匹配。"""
    return ''.join(str(s).split())


def _group_free_text_lines(texts):
    """把游离文字按基线 y 聚成视觉行。

    返回 [{'y', 'items': [{'idx', 'x', 'y', 'text'}]}], items 按 x 升序,
    idx 为在 texts 列表中的下标 (供稳定几何伪地址寻址)。
    """
    items = sorted(({'idx': i, 'x': t['x'], 'y': t['y'], 'text': t['text']}
                    for i, t in enumerate(texts)),
                   key=lambda it: (it['y'], it['x']))
    lines = []
    for it in items:
        # y 坐标差不超过 6 的文字片段归为同一视觉行
        if lines and abs(it['y'] - lines[-1]['y']) <= 6:
            lines[-1]['items'].append(it)
        else:
            lines.append({'y': it['y'], 'items': [it]})
    return lines


def _pick_header_row(cells, header_rows):
    """表头行候选中选定真表头: 取"下一行格子数最多"的候选。

    真表头直接压在最宽的表体行之上; 已填表照片里 抽检人/抽检时间 等
    信息行也可能满足短文本表头规则, 用下一行宽度把它们区分开 (并列取
    最上者)。"""
    if not header_rows:
        return None
    by_row = {}
    for c in cells:
        by_row.setdefault(c['row'], []).append(c)
    return max(sorted(header_rows),
               key=lambda r: len(by_row.get(r + 1, [])))


def _label_col(counts):
    """行标签列: 非空格计数最高者, 但照片/跨行合并场景下值列计数常会
    反超 (10 个实测子格跨两行), 取"计数 >= 60% 最大值"的最左列——
    行标签几乎总在表体最左列。"""
    if not counts:
        return None
    top = max(counts.values())
    for col in sorted(counts):
        if counts[col] >= top * 0.6:
            return col
    return min(counts)


def _table_rows_set(cells, header_row):
    """返回表头之下连续表体的行号集合，排除表单尾区。

    先按表头下方各列的非空格计数估计行标签列，再逐行向下扩展。行标签列
    左侧出现文本通常意味着进入新区块；符合“短标签 + 右侧至多一格且无
    内容”的行则更像检查结论、审核人等尾区，两种情况都终止表体。
    空白填写行本身不会截断表体，因此专用填写行和备用空行仍会保留。
    """
    by_row = {}
    for c in cells:
        by_row.setdefault(c['row'], []).append(c)
    counts = {}
    for c in cells:
        if c['row'] > header_row and (c.get('text') or '').strip():
            counts[c['col']] = counts.get(c['col'], 0) + 1
    table_rows = set()
    if counts:
        label_col = _label_col(counts)
        r = header_row + 1
        # 只沿连续行号向下扩展，遇到断行即结束表体
        while r in by_row:
            row_cells = by_row[r]
            if any((c.get('text') or '').strip()
                   for c in row_cells if c['col'] < label_col):
                break
            # 表单尾区标签行 (检查结论/审核人): 行首短文本无符号前缀,
            # 右侧整行无内容, 且右侧只有 1 个格 (尾区是整段合并格;
            # 数据行规定值/实测值/合格率是多个空格, 不得误判) -> 表体到此
            def _tail_label(c):
                t = (c.get('text') or '').replace('\n', '').strip()
                if not (t and len(t) <= 12
                        and t[0] not in _NO_LABEL_PREFIX):
                    return False
                if any((x.get('text') or '').strip()
                       for x in row_cells if x['col'] > c['col']):
                    return False
                right_cells = sum(1 for x in row_cells if x['col'] > c['col'])
                return right_cells < 2
            if any(_tail_label(c) for c in row_cells
                   if c['col'] < max(label_col, 1)):
                break
            table_rows.add(r)
            r += 1
    return table_rows


def discover_table(cells, header_rows=None):
    """统一发现表格拓扑及每个“行标签 × 表头列”的候选值格。

    header_rows 可传入外部候选行；缺省使用 role=header 的格子。没有表头时
    返回带空集合的同构结果，调用方无需另写缺表头分支。

    返回字典含 header_row/headers/table_rows/label_col/by_grid/rows。
    rows 只包含有行标签的条目行，专用空白填写行不会被误当成新条目。
    rows[].columns 保留所有非行标签表头列，target 可能是已有内容格、空格或
    None：同行已有内容会保留；填写目标优先取同行带地址的空格，仅在下邻行
    没有新行标签时才回退到下一行，避免错位到备用空行或下一条目。
    fields 后续只挑其中的空格，index 则登记全部目标并报告缺列。
    """
    if header_rows is None:
        header_rows = {c['row'] for c in cells if c.get('role') == 'header'}
    header_row = _pick_header_row(cells, header_rows)
    result = {'header_row': header_row, 'headers': {}, 'table_rows': set(),
              'label_col': None, 'by_grid': {}, 'rows': []}
    if header_row is None:
        return result

    by_grid = {(c['row'], c['col']): c for c in cells}
    headers = {}
    for c in cells:
        if c['row'] == header_row and (c.get('text') or '').strip():
            headers.setdefault(c['col'],
                               (c.get('text') or '').replace('\n', '').strip())
    table_rows = _table_rows_set(cells, header_row)
    counts = {}
    for c in cells:
        if c['row'] in table_rows and (c.get('text') or '').strip():
            counts[c['col']] = counts.get(c['col'], 0) + 1
    # 没有表体文字或表头时，不强行推断行标签列
    label_col = _label_col(counts) if counts and headers else None
    result.update(headers=headers, table_rows=table_rows,
                  label_col=label_col, by_grid=by_grid)
    if label_col is None:
        return result

    rows = []
    for r in sorted(table_rows):
        key_cell = by_grid.get((r, label_col))
        key = (key_cell.get('text') or '').replace('\n', '').strip() \
            if key_cell else ''
        if not key:
            continue
        columns = {}
        for hc, hname in headers.items():
            if hc == label_col:
                continue
            target = by_grid.get((r, hc))
            # 同行非空目标直接保留；仅空格或缺格需要重新选值格
            if target is None or not (target.get('text') or '').strip():
                target = None
                # 本行空格优先; 下一行仅在同行没有地址时回退, 避免错位到
                # 备用空行或下一已有条目。
                for rr in (r, r + 1):
                    tc = by_grid.get((rr, hc))
                    if tc is None or (tc.get('text') or '').strip() \
                            or not tc.get('addr'):
                        continue
                    if rr == r + 1:
                        lbl = by_grid.get((rr, label_col))
                        if lbl is not None \
                                and (lbl.get('text') or '').strip():
                            continue
                    target = tc
                    break
            columns[hname] = target
        rows.append({'key': key, 'key_cell': key_cell, 'columns': columns})
    result['rows'] = rows
    return result
