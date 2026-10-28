# -*- coding: utf-8 -*-
"""
ocr_service.py

把 RapidOCR (rapidocr-onnxruntime, 本地推理) 包成本地 HTTP 服务。
调用方通过 --ocr-url 或 OCR_SERVICE_URL=http://127.0.0.1:8100 选择远程
路径; 未配置时, text.ocr_cells 会在当前进程内加载模型。远程路径把整图
编码一次, 连同格子坐标批量提交, 避免逐格建立 HTTP 请求或重复加载模型。

模型常驻: 启动时加载一次 RapidOCR, 之后每个请求只做裁剪+识别。引擎
初始化和推理分别加锁: 前者防止并发请求重复构造, 后者因为 onnxruntime
session 非线程安全而把识别串行化 (以吞吐换稳定)。

用法:
    python backend/ocr_service.py [--host 127.0.0.1] [--port 8100]

接口:
    POST /ocr/cells
      请求  {"image_b64": "<整图 PNG base64>",
             "cells": [{"row":0,"col":0,"x":..,"y":..,"w":..,"h":..}, ...],
             "inset": 4}
      响应  {"results": [{"row":0,"col":0,"text":"..."}, ...]}
      只返回识别出文字的格子; 裁剪/漏检兜底/阅读顺序还原逻辑与
      text.ocr_cells 的本地路径完全一致 (复用其函数)。

本文件位于 backend/, 入口脚本把项目根 (上一级目录) 加进 sys.path 后
直接 import backend.template_pipeline 的叶子模块。
"""

import argparse
import base64
import os
import sys
import threading

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 脚本入口需先把项目根加进 sys.path 才能 import backend 包
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 复用 ocr_cells 的裁剪/阅读顺序工具与 ocr_engine 的引擎构造 (与本地路径同一实现)
from backend.template_pipeline.constants import _OCR_TEXT_SCORE
from backend.template_pipeline.text.ocr_cells import (
    _prepare_crop, _tight_crop, _reading_order)
from backend.template_pipeline.text.ocr_engine import _make_ocr_engine

app = FastAPI(title='rapidocr-local-service')
_engine = None  # RapidOCR 实例, 启动时加载一次, 常驻内存
_engine_init_lock = threading.Lock()  # ASGI 多线程首次请求只构造一次
_engine_lock = threading.Lock()  # onnxruntime session 非线程安全

# 在解码前限制 Base64 字符数、解码后限制像素数，并限制单批格子数；
# 三道上限分别控制请求体内存、位图内存和单请求占锁识别时长。
_MAX_IMAGE_B64_CHARS = 64 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_CELLS = 10_000


class CellRect(BaseModel):
    row: int
    col: int
    x: int
    y: int
    w: int
    h: int


class OcrRequest(BaseModel):
    image_b64: str  # 整图 PNG (base64), 各格子共享同一张图, 只传一次
    cells: list[CellRect]
    inset: int = 4


class CellText(BaseModel):
    row: int
    col: int
    text: str


class OcrResponse(BaseModel):
    results: list[CellText]


def _get_engine():
    """返回进程级 RapidOCR 实例，必要时用双重检查锁完成一次性初始化。

    已初始化时不进入锁，避免给每个请求增加额外同步；构造失败或缺少
    rapidocr-onnxruntime 时不发布半成品实例。依赖缺失以 503 告知调用方，
    阈值和 GPU/CPU 选择均由本地路径共用的 _make_ocr_engine 决定。
    """
    global _engine
    if _engine is None:
        with _engine_init_lock:
            if _engine is None:
                engine = _make_ocr_engine()
                if engine is None:
                    raise HTTPException(
                        status_code=503,
                        detail='未安装 rapidocr-onnxruntime '
                               '(pip install rapidocr-onnxruntime)')
                _engine = engine
    return _engine


@app.post('/ocr/cells', response_model=OcrResponse)
def ocr_cells_endpoint(req: OcrRequest) -> OcrResponse:
    """批量逐格识别，复用 ocr_cells 的本地识别链路和返回约定。

    整图解码后按原图坐标裁剪（内缩避边框、空格跳过、极端长宽比补白、
    小格放大），检测漏检时退化为紧贴内容的整格识别。只返回识别到文字的
    格子；单格失败不中断整个批次，也不伪造空文本结果。

    所有识别调用在全局锁内串行执行；锁覆盖整个批次而非单次模型调用，
    因此同一时刻只有一个请求占用引擎，其余请求等待。输入在模型推理前经过
    格子数、Base64 长度、可解码性及像素数检查，避免异常请求长期占用推理锁。
    """
    if len(req.cells) > _MAX_CELLS:
        raise HTTPException(status_code=400,
                            detail=f'cells 最多 {_MAX_CELLS} 个')
    if len(req.image_b64) > _MAX_IMAGE_B64_CHARS:
        raise HTTPException(status_code=413, detail='image_b64 超过 64MB 上限')
    try:
        # validate=True 拒绝非 Base64 字符，避免宽松解码掩盖损坏请求。
        raw = base64.b64decode(req.image_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400,
                            detail='image_b64 不是合法 base64') from exc
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail='image_b64 无法解码为图像')
    # Base64 长度无法反映解压后的位图大小，必须在解码后再次限制像素数。
    if img.shape[0] * img.shape[1] > _MAX_IMAGE_PIXELS:
        raise HTTPException(status_code=413,
                            detail=f'图像像素超过 {_MAX_IMAGE_PIXELS} 上限')
    # 初始化在推理锁外完成；两把锁分别保护模型构造和模型调用。
    engine = _get_engine()
    results = []
    # 一个请求独占整个批次，防止并发请求交叉调用同一组 ONNX session。
    with _engine_lock:
        for c in req.cells:
            crop = _prepare_crop(img, c.model_dump(), req.inset)
            if crop is None:
                continue
            try:
                res, _ = engine(crop)
            except Exception:
                res = None
            if res:
                results.append(CellText(row=c.row, col=c.col, text='\n'.join(
                    _reading_order(res))))
                continue
            # 检测漏检 (单字/连体手写等): 紧贴内容, 整格作为一行直接识别
            sub = _tight_crop(crop)
            if sub is None:
                continue
            try:
                rec_res, _ = engine.text_recognizer([sub])
            except Exception:
                continue
            if rec_res and float(rec_res[0][1]) >= _OCR_TEXT_SCORE:
                # 仅保留达到置信度阈值的文本，空格和低置信度格不出现在响应中。
                results.append(CellText(row=c.row, col=c.col,
                                        text=rec_res[0][0]))
    return OcrResponse(results=results)


def main() -> None:
    """解析监听参数，预热常驻模型后启动 ASGI 服务。"""
    ap = argparse.ArgumentParser(description='RapidOCR 本地服务')
    ap.add_argument('--host', default='127.0.0.1', help='监听地址 (默认 127.0.0.1)')
    ap.add_argument('--port', type=int, default=8100, help='监听端口 (默认 8100)')
    args = ap.parse_args()
    _get_engine()  # 启动即加载模型, 首个请求不背冷启动
    print(f'RapidOCR 服务监听 http://{args.host}:{args.port} (POST /ocr/cells)')
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
