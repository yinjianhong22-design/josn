# -*- coding: utf-8 -*-
"""从页面二值图分离内容并分析文字、图片区域。

输入的前景像素为白色；先扣除表格线得到纯内容掩码，再在其上完成内容块
分类和文字行切分。所有输出矩形及行坐标均相对整页，而非某个局部块。
"""

# ---------------------------------------------------------------------------
# 版面分析 (文字区域 / 图片块 / 行切分)
# ---------------------------------------------------------------------------

import cv2
import numpy as np


def analyze_layout(bw, horiz_c, vert_c, min_area=200):
    """分析页面内容边界及文字块、图片块外接矩形。

    从二值图中剔除闭运算后的表格线，得到纯内容掩码；再用随页面尺寸调整的
    闭运算核聚合邻近字符并提取外轮廓。轮廓外接框内原始内容填充密度大于
    0.5 的块标为 image，其余标为 text；这是几何启发式分类，不做语义识别。
    返回 (content, blocks, bounds)：content 为与原图同尺寸的内容掩码，blocks
    为按 y、x 排序的块字典列表，每块含 x、y、w、h、type；bounds 是最外内容
    像素的整页边界，无内容时为 None。
    """
    content = bw.copy()
    # 表格线不属于内容，先从副本掩码中扣除，避免污染轮廓和密度统计。
    content[horiz_c > 0] = 0
    content[vert_c > 0] = 0
    h, w = content.shape
    # 闭运算核随页面尺寸缩放，横向通常更长，以聚合同一行内的相邻字符。
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                       (max(15, w // 80), max(8, h // 150)))
    merged = cv2.morphologyEx(content, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    blocks = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw * ch < min_area:
            continue
        # 分类密度取自未聚合的 content，避免闭运算膨胀把稀疏文字也判成图片。
        density = content[y:y + ch, x:x + cw].mean() / 255
        blocks.append({'x': int(x), 'y': int(y), 'w': int(cw), 'h': int(ch),
                       'type': 'image' if density > 0.5 else 'text'})
    blocks.sort(key=lambda b: (b['y'], b['x']))

    # 内容边界取自未膨胀的原始前景，不使用闭运算后的 merged。
    cols = np.where(content.any(axis=0))[0]
    rows = np.where(content.any(axis=1))[0]
    bounds = None
    if cols.size and rows.size:
        bounds = {'x': int(cols[0]), 'y': int(rows[0]),
                  'w': int(cols[-1] - cols[0]), 'h': int(rows[-1] - rows[0])}
    return content, blocks, bounds


def split_text_lines(content, rect, gap=2, min_h=5):
    """在 rect=(x, y, w, h) 内按横向投影切分文字行。

    将逐行是否有前景像素压成布尔序列，并按 gap 向前探测、桥接短空白，
    避免字内断裂把同一视觉行拆开；高度小于 min_h 的段视为噪点。返回整页
    y 坐标的半开区间 [(y0, y1), ...]，行高 y1-y0 可约束还原字号：
    size_pt ≈ 行高*72/dpi*0.75。
    """
    x, y, w, h = rect
    sub = content[y:y + h, x:x + w]
    if sub.size == 0:
        return []
    on = (sub > 0).sum(axis=1) > 0
    runs = []
    i, n = 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j + 1 < n and (on[j + 1] or
                                 (j + gap < n and on[j + 1:j + gap + 1].any())):
                j += 1
            if j - i + 1 >= min_h:
                # 局部切片下标加回 y，输出整页坐标的半开行区间。
                runs.append((y + i, y + j + 1))
            i = j + 1
        else:
            i += 1
    return runs
