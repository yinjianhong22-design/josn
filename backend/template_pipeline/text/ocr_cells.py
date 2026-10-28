# -*- coding: utf-8 -*-

"""表格单元格 OCR：本地 RapidOCR 与 HTTP 服务两条路径的统一实现。

未配置服务地址时，在当前进程按需构造并缓存引擎；配置 ``--ocr-url`` 或
``OCR_SERVICE_URL`` 后，整图和格子坐标批量交给服务处理。两条路径采用同一套
裁剪、检测阅读顺序及整格识别兜底规则，并返回 ``{(row, col): text}``。
"""

import base64
import json
import os
import urllib.error
import urllib.request
from functools import lru_cache

import cv2
import numpy as np

from ..constants import _OCR_TEXT_SCORE
from .ocr_engine import _make_ocr_engine


@lru_cache(maxsize=None)
def _get_ocr_engine(use_gpu=None):
    """按 GPU 构造参数缓存本地引擎，首次实际 OCR 时才加载模型。

    远程 HTTP 路径不经过这里，因此服务调用方无需在本进程常驻模型；
    ``use_gpu=None`` 沿用引擎工厂的自动 CUDA 探测与 CPU 回退策略。
    """
    return _make_ocr_engine(use_gpu=use_gpu)


def _tight_crop(crop, margin=8):
    """以深色像素的内容包围盒裁掉留白，并保留少量边距。

    仅用于文字检测失败后的整格识别兜底：减少边框和大片空白对单行识别器
    的干扰；没有深色像素时返回 None，让调用方跳过该格。
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # np.where 返回 (y, x) 两组下标，转成 N×2 后统一计算内容包围盒。
    dark = np.column_stack(np.where(gray < 128))
    if dark.size == 0:
        return None
    y0, x0 = dark.min(axis=0)
    y1, x1 = dark.max(axis=0)
    return crop[max(0, y0 - margin):y1 + margin,
                max(0, x0 - margin):x1 + margin]


def _prepare_crop(img, c, inset=4):
    """按原图像素坐标准备单格 OCR 图像，空格或无效区域返回 None。

    先内缩以避开表格边框，再用深色像素数量跳过近空白格。极端长宽比通过
    上下或左右补白压到约 5:1，而非拉伸文字，降低检测器退化为整格单行的
    概率；过小的图再等比放大，且不缩小大图。服务与本地路径共用此函数，
    因此坐标和预处理行为一致。
    """
    x0, y0 = c['x'] + inset, c['y'] + inset
    x1, y1 = c['x'] + c['w'] - inset, c['y'] + c['h'] - inset
    if x1 <= x0 or y1 <= y0:
        return None
    crop = img[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if int((gray < 128).sum()) < 30:
        return None  # 深色像素太少, 格内基本没有内容
    ch, cw = crop.shape[:2]
    if cw > ch * 5:
        # 极宽格在上下补白，保持文字比例并避免检测器把它当作单行文本。
        pad = (cw // 5 - ch) // 2
        crop = cv2.copyMakeBorder(crop, pad, pad, 0, 0,
                                  cv2.BORDER_CONSTANT, value=(255, 255, 255))
    elif ch > cw * 5:
        # 极高格则在左右补白。
        pad = (ch // 5 - cw) // 2
        crop = cv2.copyMakeBorder(crop, 0, 0, pad, pad,
                                  cv2.BORDER_CONSTANT, value=(255, 255, 255))
    # 小格放大再识别: 检测/识别模型对短边过小的图会系统性误读
    # (实测 "99%" 在 72px 宽格内被读成 "666", 放大到 ~300px 后正确),
    # 只放大不缩小, 放大倍数封顶 4
    ch, cw = crop.shape[:2]
    short = max(ch, cw)
    if short < 300:
        k = min(4, 300 / short)
        crop = cv2.resize(crop, None, fx=k, fy=k,
                          interpolation=cv2.INTER_CUBIC)
    return crop


def _reading_order(res):
    """把 RapidOCR 的有框检测结果还原为按阅读顺序排列的行文本列表。

    先取每个四点框的中心和高度，再按中心 y 从上到下贪心聚行：与已有行
    中心差不超过 max(6px, 当前行高*0.7) 就归入该行，并更新平均中心 y 和
    最大行高；行内最终按中心 x 排序拼接。返回值保留行边界，由调用方用换行
    连接，而不是在几何排序前直接拼成一整段。

    只按 y 全局排序会把同一行里 y 略有高低差的多个框排反（小格中符号框与
    汉字框错位常见），产生“(%)xxx”这类倒序串；相对行高容差也能适配不同
    缩放级别。这是通用阅读顺序恢复，不依赖特定文字内容。
    """
    items = []
    for box, t, _ in res:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        # 四点框可能有透视倾斜，用中心点排序、用实际框高估计行容差。
        items.append({'cx': sum(xs) / 4.0, 'cy': sum(ys) / 4.0,
                      'h': max(ys) - min(ys), 't': t})
    lines = []
    for it in sorted(items, key=lambda d: (d['cy'], d['cx'])):
        for line in lines:
            if abs(line['cy'] - it['cy']) <= max(6.0, line['h'] * 0.7):
                line['items'].append(it)
                n = len(line['items'])
                # 新框加入后更新行中心均值和最大行高，供后续框继续聚类。
                line['cy'] = (line['cy'] * (n - 1) + it['cy']) / n
                line['h'] = max(line['h'], it['h'])
                break
        else:
            lines.append({'cy': it['cy'], 'h': it['h'], 'items': [it]})
    lines.sort(key=lambda l: l['cy'])
    return [''.join(i['t'] for i in sorted(line['items'],
                                           key=lambda d: d['cx']))
            for line in lines]


def ocr_cells(img, cells, inset=4):
    """在当前进程逐格执行 RapidOCR，返回 {(row, col): 文本}。

    整图一次识别容易在表格场景漏检或串行，故先按格子坐标裁剪，再执行
    “文字检测+识别”。检测框按 _reading_order 恢复阅读顺序；单字、连体手写
    等导致检测无结果时，裁紧内容并绕过检测，直接把整格作为一行识别。

    单格异常只记录警告并跳过或尝试兜底，不中断其他格子。未安装
    rapidocr-onnxruntime 时返回 None，以便上层跳过 OCR、仅还原表格结构。
    """
    engine = _get_ocr_engine()
    if engine is None:
        return None
    texts = {}
    for c in cells:
        crop = _prepare_crop(img, c, inset)
        if crop is None:
            continue
        try:
            res, _ = engine(crop)
        except Exception as e:
            print(f'OCR 警告: 格[{c["row"]},{c["col"]}] 检测识别失败 '
                  f'({type(e).__name__}: {e}), 尝试整格识别')
            res = None
        if res:
            texts[(c['row'], c['col'])] = '\n'.join(_reading_order(res))
            continue
        # 检测漏检 (单字/连体手写等): 紧贴内容, 整格作为一行直接识别
        sub = _tight_crop(crop)
        if sub is None:
            continue
        try:
            rec_res, _ = engine.text_recognizer([sub])
        except Exception as e:
            print(f'OCR 警告: 格[{c["row"]},{c["col"]}] 整格识别失败 '
                  f'({type(e).__name__}: {e}), 跳过此格')
            continue
        # 整格识别没有经过检测阶段，必须单独按识别置信度过滤低质量结果。
        if rec_res and float(rec_res[0][1]) >= _OCR_TEXT_SCORE:
            texts[(c['row'], c['col'])] = rec_res[0][0]
    return texts


def _ocr_cells_remote(img, cells, inset, base_url, timeout=600):
    """把整图和格子坐标批量转发给 OCR 服务，返回 {(row, col): 文本}。

    客户端只做无损 PNG/Base64 编码，裁剪、检测漏检兜底和阅读顺序均在服务端
    复用本地路径的函数完成；格子坐标保持原图坐标系，行、列键用于还原结果。
    相比逐格请求，整图只编码和传输一次，也避免客户端进程加载 OCR 模型。

    服务不可达或返回非 2xx 时抛出含地址与原因的 RuntimeError，不静默回退到
    本地模型，以免配置错误被掩盖。timeout 默认 600s，因为服务端会在全局锁内
    串行识别，大表或排队请求可能耗时较长。
    """
    ok, buf = cv2.imencode('.png', img)
    if not ok:
        raise RuntimeError('OCR 服务调用失败: 整图 PNG 编码失败')
    payload = {
        # PNG 避免 JPEG 二次压缩损伤文字笔画；Base64 仅用于嵌入 JSON 请求体。
        'image_b64': base64.b64encode(buf.tobytes()).decode('ascii'),
        'cells': [{'row': c['row'], 'col': c['col'], 'x': int(c['x']),
                   'y': int(c['y']), 'w': int(c['w']), 'h': int(c['h'])}
                  for c in cells],
        'inset': inset,
    }
    url = base_url.rstrip('/') + '/ocr/cells'
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'OCR 服务返回 {e.code}: {url}') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f'OCR 服务不可达 ({url}): {e}') from e
    return {(int(r['row']), int(r['col'])): r['text']
            for r in body.get('results', [])}


def _pick_ocr_backend(args):
    """OCR 后端: 返回 (名称, (img, cells) -> dict)。

    --ocr-url / 环境变量 OCR_SERVICE_URL 非空 → 批量提交给本地 OCR 服务
    (ocr_service.py), 服务不可达时 _ocr_cells_remote 直接抛错, 不静默回退;
    未配置 → 进程内 RapidOCR 逐格识别 (未安装 rapidocr-onnxruntime 时
    ocr_cells 返回 None, 跳过文字识别只还原结构)。
    """
    base_url = (getattr(args, 'ocr_url', None)
                or os.environ.get('OCR_SERVICE_URL', ''))
    if base_url:
        # 命令行参数优先于环境变量；远程错误直接向上抛出，不隐式切换本地。
        return ('RapidOCR (服务)',
                lambda img, cells: _ocr_cells_remote(img, cells, 4, base_url))
    return 'RapidOCR (本地)', ocr_cells


def _attach_line_pos(cells, texts):
    """把 OCR 出的每行文字对号到投影切分出的行位置上。

    OCR 逐格返回多行文本但不含坐标; 这里用横向投影的行切分结果
    (cell['text_lines']) 按顺序对位, 供 PDF 还原按行高定字号和位置。
    行数对不上时放弃该格的位置约束, 退回整体自动适配。
    """
    for c in cells:
        t = texts.get((c['row'], c['col']))
        spans = c.get('text_lines') or []
        if not t or len(spans) != len(t.split('\n')):
            # 只有行数完全一致才能可靠一一对应，否则交由后续整体适配。
            continue
        c['lines'] = [{'y': y0, 'h': y1 - y0, 'text': txt}
                      for (y0, y1), txt in zip(spans, t.split('\n'))]
