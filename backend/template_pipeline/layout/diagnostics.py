# -*- coding: utf-8 -*-
"""表格识别的可视化与线诊断输出。

本模块只消费识别结果并生成调试产物，不参与线检测、格子重建或文字识别；
输出中的几何字段沿用各识别阶段的整页像素坐标。
"""

# ---------------------------------------------------------------------------
# 可视化 + 线诊断落盘
# ---------------------------------------------------------------------------

import json
import os

import cv2


def draw_overlay(img, h_pos, v_pos, cells, out_path, blocks=None):
    """把内容块、检测线和重建格叠加到原图副本并写入 out_path。

    不修改传入图像。颜色采用 OpenCV BGR：横线绿、竖线蓝、格子红；内容块
    另按 text/image 区分，格内标注的是原子网格的 row、col 左上角索引。
    """
    vis = img.copy()
    for b in blocks or []:
        color = (200, 0, 200) if b['type'] == 'text' else (0, 165, 255)
        cv2.rectangle(vis, (b['x'], b['y']),
                      (b['x'] + b['w'], b['y'] + b['h']), color, 2)
    # 检测线按整页画辅助线，仅用于定位线坐标，不表示真实线段的延伸范围。
    for y in h_pos:
        cv2.line(vis, (0, y), (vis.shape[1] - 1, y), (0, 255, 0), 1)
    for x in v_pos:
        cv2.line(vis, (x, 0), (x, vis.shape[0] - 1), (255, 0, 0), 1)
    for c in cells:
        cv2.rectangle(vis, (c['x'], c['y']),
                      (c['x'] + c['w'], c['y'] + c['h']), (0, 0, 255), 1)
        cv2.putText(vis, f"{c['row']},{c['col']}",
                    (c['x'] + 3, c['y'] + 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
    # imwrite 按扩展名选择编码，输出目录需由调用方提前创建。
    cv2.imwrite(out_path, vis)


def dump_lines_json(out_dir, result):
    """把线诊断结果原样写入 out_dir/lines.json（打印内容不变）。

    result 为 extract_page 组好的诊断 dict，通常含线坐标、交点、格子和版面
    块；本函数不重新计算或修正这些字段，调用方需保证其可被 JSON 序列化。
    """
    json_path = os.path.join(out_dir, 'lines.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        # 保留中文并格式化缩进，便于直接阅读和版本间 diff。
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'结果已写入 {json_path}')

