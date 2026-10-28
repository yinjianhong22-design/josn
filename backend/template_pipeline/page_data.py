# -*- coding: utf-8 -*-
"""构建表格还原链路的标准整页数据（data.json）。

本层只组装识别结果并复核格子边框，不执行 OCR 或重新推断网格。所有几何值
保持整页像素坐标，dpi 供下游换算物理尺寸、字号等展示单位。
"""

# ---------------------------------------------------------------------------
# 标准 JSON (data.json) —— 表格还原的唯一数据源
# ---------------------------------------------------------------------------

from importlib import import_module as _import_module

_cells = _import_module('.layout.cells', __package__)
edge_present = _cells.edge_present


def build_table_data(cells, texts, horiz_c, vert_c, dpi, img_size,
                     free_texts=None, rules=None):
    """把格子重建、OCR 与线掩码结果组织成标准 data.json 数据结构。

    输入 texts 是 {(row, col): 格内文本} 映射；输出顶层 texts 则是格外游离
    文字列表，两者名称相同但契约不同。cells 使用 find_cells 的原子网格索引
    和整页几何，img_size 为 (宽, 高)，horiz_c/vert_c 为最终横竖线掩码。

    输出顶层固定包含 dpi、width、height、cells、texts、rules。每个格子包含
    row、col、rowspan、colspan、x、y、w、h、borders、text；borders 的四条边
    由线掩码重新检测并转为布尔值，不从合并跨度反推。OCR 行级信息 lines 和
    主导字体 font 仅在上游提供时附加。texts 保存带基线坐标和字号的页眉、
    标题、页码等游离文字，rules 保留兼容字段。零格子时也返回合法结构。
    """
    def edges(c):
        # 用合并格外框四角复核四边，边框存在性由最终线掩码决定。
        tl = (c['x'], c['y'])
        tr = (c['x'] + c['w'], c['y'])
        bl = (c['x'], c['y'] + c['h'])
        br = (c['x'] + c['w'], c['y'] + c['h'])
        # 显式转为原生 bool，保证四边字段可直接序列化为标准 JSON。
        return {
            'top': bool(edge_present(horiz_c, tl, tr, 'h')),
            'bottom': bool(edge_present(horiz_c, bl, br, 'h')),
            'left': bool(edge_present(vert_c, tl, bl, 'v')),
            'right': bool(edge_present(vert_c, tr, br, 'v')),
        }

    w_img, h_img = img_size
    cell_list = []
    for c in cells:
        # 保留原子网格索引、合并跨度和整页几何，OCR 文本按格坐标对位。
        d = {
            'row': c['row'], 'col': c['col'],
            'rowspan': c['rowspan'], 'colspan': c['colspan'],
            'x': c['x'], 'y': c['y'],
            'w': c['w'], 'h': c['h'],
            'borders': edges(c),
            'text': texts.get((c['row'], c['col']), ''),
        }
        if c.get('lines'):
            # OCR 行级信息: 行高、基线估算与文本
            d['lines'] = c['lines']
        if c.get('font'):
            d['font'] = c['font']  # 主导字体名, 供还原时映射本机字体
        cell_list.append(d)

    # 顶层字段保持类型稳定；未提供的游离文字和规则统一归一为空列表。
    return {
        'dpi': dpi,
        'width': w_img,
        'height': h_img,
        'cells': cell_list,
        'texts': free_texts or [],
        'rules': rules or [],
    }
