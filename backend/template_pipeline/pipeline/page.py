# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# 单页图片处理主流程: 线检测 -> 格子 -> 文字 -> 标准 JSON data
# ---------------------------------------------------------------------------

import os
from importlib import import_module as _import_module

import cv2

from ..page_data import build_table_data

_loader = _import_module('..input.loader', __package__)
load_image = _loader.load_image
_lines_pixel = _import_module('..layout.lines_pixel', __package__)
binarize = _lines_pixel.binarize
extract_line_masks = _lines_pixel.extract_line_masks
close_gaps = _lines_pixel.close_gaps
find_line_positions = _lines_pixel.find_line_positions
filter_dangling_lines = _lines_pixel.filter_dangling_lines
_cells = _import_module('..layout.cells', __package__)
compute_intersections = _cells.compute_intersections
find_cells = _cells.find_cells
_content = _import_module('..layout.content', __package__)
analyze_layout = _content.analyze_layout
split_text_lines = _content.split_text_lines
_diagnostics = _import_module('..layout.diagnostics', __package__)
draw_overlay = _diagnostics.draw_overlay
dump_lines_json = _diagnostics.dump_lines_json
_ocr_cells = _import_module('..text.ocr_cells', __package__)
_pick_ocr_backend = _ocr_cells._pick_ocr_backend
_attach_line_pos = _ocr_cells._attach_line_pos
_ocr_free = _import_module('..text.ocr_free', __package__)
ocr_free_blocks = _ocr_free.ocr_free_blocks
_classify_line_roles = _ocr_free._classify_line_roles


def extract_page(input_path, page, args, diagnostics=True):
    """完成单页图像的几何重建与 OCR，返回后续语义阶段使用的标准 data。

    总调度分为四步：加载并二值化图像；提取、修补和过滤表格线，再求交点
    与单元格；结合版面块及格内投影行执行格内、格外 OCR；最后汇总几何、
    文字和游离文本，并标记 source_page。角色判定、字段提取和索引构建由
    后续语义阶段完成，不在本函数内耦合。

    args.no_ocr 为真时仅保留几何与版面结果。diagnostics=True 时输出统计
    信息；args.out 非空时额外写出 overlay.png 和 lines.json。
    """
    img = load_image(input_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    bw = binarize(gray)
    horiz, vert = extract_line_masks(bw, h_ratio=args.h_ratio,
                                     v_ratio=args.v_ratio)
    # 交点判定和格子边判定用接合断口后的掩码, 避免断线处的误判
    horiz_c = close_gaps(horiz, 'h')
    vert_c = close_gaps(vert, 'v')
    # 自适应跨度参数可缺省；显式传入的阈值始终优先。
    h_pos = find_line_positions(horiz_c, 'h', min_span=args.h_min_span,
                                span_ratio=getattr(args, 'h_span_ratio',
                                                   0.45))
    v_pos = find_line_positions(vert_c, 'v', min_span=args.v_min_span,
                                span_ratio=getattr(args, 'v_span_ratio',
                                                   0.20))
    # 表格线的两端必然落在对向线上; 两端悬空的(页眉下划线等)滤掉
    h_pos = filter_dangling_lines(h_pos, horiz_c, 'h', v_pos,
                                  other_mask=vert_c)
    v_pos = filter_dangling_lines(v_pos, vert_c, 'v', h_pos,
                                  other_mask=horiz_c)

    intersections = compute_intersections(h_pos, v_pos, horiz_c, vert_c)
    cells = find_cells(h_pos, v_pos, horiz_c, vert_c)

    # 版面分析: 页面内容边界 + 文字块/图片块; 每格做横向投影行切分,
    # 行切分结果用于约束 OCR 文字还原时的字号和位置
    content, blocks, bounds = analyze_layout(bw, horiz_c, vert_c)
    # 每格向内缩 4px 再切行，避开边框残线；切分结果仍使用整页 y 坐标。
    for c in cells:
        c['text_lines'] = split_text_lines(
            content, (c['x'] + 4, c['y'] + 4, c['w'] - 8, c['h'] - 8))

    # OCR 分为格内与格外两路：前者按 (row, col) 对位，后者保留为游离文本，
    # 使标题、页眉页脚等不被单元格拓扑丢弃。
    texts = {}
    free_texts = []
    if not args.no_ocr:
        name, backend = _pick_ocr_backend(args)
        print(f'OCR 后端: {name} (逐格请求)')
        ocr_res = backend(img, cells)
        if ocr_res is None:
            print('未安装 rapidocr-onnxruntime, 跳过文字识别'
                  ' (pip install rapidocr-onnxruntime)')
        else:
            texts = ocr_res
            # 投影行切分对位: 约束每行文字还原时的字号和位置
            _attach_line_pos(cells, texts)
            # 表格线框外的文字块也 OCR, 按行高估算字号作为游离文字
            free_texts = ocr_free_blocks(img, blocks, cells, content,
                                         backend, args.dpi)
            if free_texts:
                # 角色分类临时需要 cx；构造分类输入时将其移出标准游离文本。
                tmp = [{'size': t['size'], 'cx': t.pop('cx')}
                       for t in free_texts]
                _classify_line_roles(tmp, img.shape[1])
                for t, r in zip(free_texts, tmp):
                    # 两列表顺序一致，只回写 role，不改动 OCR 几何和文本。
                    t['role'] = r['role']
                print(f'格外游离文字 {len(free_texts)} 行 (OCR)')
        if texts and diagnostics:
            print(f'文字提取完成, {len(texts)}/{len(cells)} 个格子有文字')
            for c in cells:
                t = texts.get((c['row'], c['col']))
                if t:
                    print(f'  格[{c["row"]},{c["col"]}]: {t}')

    # 统一汇总为标准 page data；此后语义模块只消费 data，不再依赖图像或
    # OCR 后端，便于空模板解析与已填表识别复用同一几何结果。
    data = build_table_data(cells, texts, horiz_c, vert_c, args.dpi,
                            (img.shape[1], img.shape[0]), free_texts, [])
    data['source_page'] = page

    if diagnostics:
        n_txt = sum(1 for b in blocks if b['type'] == 'text')
        print(f'版面分析: 内容边界 {bounds}, 文字块 {n_txt} 个, '
              f'图片块 {len(blocks) - n_txt} 个')
        result = {
            'image_size': {'width': img.shape[1], 'height': img.shape[0]},
            'horizontal_lines': h_pos,
            'vertical_lines': v_pos,
            'intersections': intersections,
            'cells': cells,
            'layout': {'content_bounds': bounds, 'blocks': blocks},
        }
        # result['cells'] 与 cells 共用列表，回填后诊断 JSON 也包含格内文本。
        for c in cells:
            c['text'] = texts.get((c['row'], c['col']), '')
        if getattr(args, 'out', None):  # 服务端调用无 out, 跳过落盘
            overlay_path = os.path.join(args.out, 'overlay.png')
            draw_overlay(img, h_pos, v_pos, cells, overlay_path, blocks)
            print(f'横线 {len(h_pos)} 条, 竖线 {len(v_pos)} 条, '
                  f'交点 {len(intersections)} 个, 格子 {len(cells)} 个')
            dump_lines_json(args.out, result)
            print(f'可视化: {overlay_path}')
    return data
