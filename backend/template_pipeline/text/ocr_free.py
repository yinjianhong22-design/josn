# -*- coding: utf-8 -*-

"""扫描件/图片中表格线框外文字的 OCR 与行位置恢复。

OCR 后端由调用方注入，因此本地引擎和 HTTP 服务共用这里的格外文字筛选、
切行及坐标换算逻辑；输出结构与 PDF 文字层提取的游离文字保持一致。
"""

from importlib import import_module as _import_module

from .line_roles import _classify_line_roles

_content = _import_module('..layout.content', __package__)
split_text_lines = _content.split_text_lines


def ocr_free_blocks(img, blocks, cells, content, backend, dpi):
    """OCR 表格线框外的文字块，并恢复为带坐标的游离文字行。

    非文字块以及中心落在任一单元格内的块不再识别，避免与逐格 OCR 重复。
    其余块通过注入的 ``backend`` 批量识别；本地和远程后端都以伪格坐标
    ``(row, col)`` 返回文本，这里据此重新对应原文字块。

    每个块按横向投影切行，行高换算字号 (size_pt = 行高*72/dpi*0.75)，
    基线取行底上方 80% 处。最终按从上到下、同行从左到右排序，产出结构与
    PDF 文字层路径的游离文字一致。
    """
    pseudo = []
    for k, b in enumerate(blocks):
        if b['type'] != 'text':
            continue
        cx, cy = b['x'] + b['w'] / 2, b['y'] + b['h'] / 2
        # 用块中心判断归属，避免只与表格边缘相交的标题被误排除；
        # 2px 容差吸收线框和块坐标的取整误差，中心在格内则交给逐格 OCR。
        if any(c['x'] - 2 <= cx <= c['x'] + c['w'] + 2
               and c['y'] - 2 <= cy <= c['y'] + c['h'] + 2 for c in cells):
            continue
        # row=-1 表示不是表格单元格；col=k 只作为本批次的稳定回取键，
        # 真正的页面位置仍使用原块坐标，不能让后端键参与几何计算。
        pseudo.append({'row': -1, 'col': k, 'x': b['x'], 'y': b['y'],
                       'w': b['w'], 'h': b['h']})
    if not pseudo:
        return []
    print(f'格外文字块 {len(pseudo)} 个, OCR 中...')
    res = backend(img, pseudo) or {}
    free = []
    for p in pseudo:
        # 用构造伪格时的 (row, col) 取回文本，再恢复原块的真实页面坐标。
        t = res.get((p['row'], p['col']))
        if not t:
            continue
        ols = t.split('\n')
        spans = split_text_lines(content, (p['x'], p['y'], p['w'], p['h']))
        if len(spans) != len(ols):
            # OCR 行数与投影行数不一致时不能 zip 对齐，否则尾部会被静默丢掉；
            # 按原块高度均分，保证每个 OCR 文本行仍有可用的位置估计。
            step = p['h'] / len(ols)
            spans = [(p['y'] + int(i * step), p['y'] + int((i + 1) * step))
                     for i in range(len(ols))]
        for (y0, y1), txt in zip(spans, ols):
            lh = y1 - y0
            # y 保存估算基线而非行顶；字号按 dpi 将像素行高换算为磅值。
            free.append({'x': p['x'], 'y': y0 + lh * 0.8,
                         'size': lh * 72 / dpi * 0.75,
                         'color': '#000000', 'bold': False, 'role': 'body',
                         'text': txt, 'cx': p['x'] + p['w'] / 2})
    free.sort(key=lambda t: (t['y'], t['x']))
    return free
