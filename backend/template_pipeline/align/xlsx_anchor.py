# -*- coding: utf-8 -*-
"""为图片 OCR 通路补齐语义配对所需的地址契约。

原生表格与图片识别结果共用字段提取、索引构建链路，但后者没有天然单元格
地址。本模块只适配这一数据入口，不负责字段识别或几何计算。
"""

# ---------------------------------------------------------------------------
# 语义对齐: 图片 OCR 通路的几何伪地址
# ---------------------------------------------------------------------------

from ..semantic.table_topology import _group_free_text_lines


def _ensure_pseudo_addr(data):
    """原地补齐图片 OCR 结果中缺失的 addr，不覆盖已有地址或返回新副本。

    extract_fields/build_index 的配对链路都依赖 addr，缺 addr 的格子会被
    丢弃。格子按文档像素尺寸与 row/col 寻址；游离文字按几何行及行内 x 序
    寻址。相同输入的结果确定，但这些几何伪地址只作为当前文档内的配对键，
    不是 Excel A1 地址，也不应作为跨文档或跨版面版本的永久主键。
    """
    # 页尺寸作为盐值区分几何版本，伪地址只在当前文档内使用。
    salt = f"{data.get('width', 0)}x{data.get('height', 0)}"
    for c in data.get('cells') or []:
        if not c.get('addr'):
            # #G 与下游游离文字的 #T 分属不同命名空间，避免地址碰撞。
            c['addr'] = f"#G{salt}:{c['row']},{c['col']}"
    # 游离文字按几何行和行内 x 序寻址, 不依赖 texts 的输入列表下标。
    # 同行同坐标重复项再按稳定排序后的序号消歧, 不引入随机量。
    for ln in _group_free_text_lines(data.get('texts') or []):
        # 坐标保留一位小数，减弱 OCR 浮点抖动对配对键的影响。
        y = round(ln['y'], 1)
        ordered = sorted(ln['items'],
                         key=lambda it: (it['x'], it['y'], it['text']))
        for i, it in enumerate(ordered):
            t = data['texts'][it['idx']]
            if not t.get('addr'):
                x = round(it['x'], 1)
                t['addr'] = f"#T{salt}:y{y}:x{x}:{i}"
