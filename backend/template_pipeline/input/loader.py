# -*- coding: utf-8 -*-
"""图像文件加载与 OpenCV 位图契约。

下游统一接收 BGR 三通道 uint8 数组；路径解码和读取失败处理集中在输入层，
避免识别算法混入平台相关的文件 I/O 差异。
"""

# ---------------------------------------------------------------------------
# 加载: 图片 -> 位图
# ---------------------------------------------------------------------------

import sys

import cv2
import numpy as np


def load_image(path):
    """加载图片并返回 OpenCV BGR 位图；解码失败时终止，不向下游返回 None。"""
    # cv2.imread 在 Windows 上走 ANSI 代码页, 中文路径读不出来;
    # imdecode(fromfile) 按字节读盘, 与路径编码无关。
    # IMREAD_COLOR 将灰度、彩色及带透明通道的输入统一解码为三通道 BGR。
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f'无法读取图片: {path}')
    return img
