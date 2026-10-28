# -*- coding: utf-8 -*-

"""构造本地 RapidOCR 引擎，并统一处理 CUDA 初始化、兼容补丁和 CPU 回退。

进程内逐格 OCR 与独立 OCR HTTP 服务都从这里创建引擎，确保模型参数和
文本阈值一致。GPU 自动模式不仅检查 provider 是否列出，还会验证 det/cls/rec
三个实际 session，避免 CUDA 依赖加载失败后静默落到不符合预期的配置。
"""

import os

from ..constants import _OCR_TEXT_SCORE


# DLL 目录/库句柄必须活到 CUDA provider 加载之后。
_CUDA_DLL_DIRECTORY_HANDLES = []
_CUDA_DLL_HANDLES = []


def _prepend_nvidia_cuda_dlls():
    """注册 pip 安装的 CUDA/cuDNN 运行库，供 onnxruntime GPU provider 使用。

    onnxruntime-gpu >= 1.24 的 Windows wheel 不再捆绑这些 DLL；这里查找与
    onnxruntime 同级的 nvidia/*/bin 目录，同时修改 PATH 和 DLL 搜索目录。
    Windows 上还需显式加载直接依赖，以兼容 provider 的 LoadLibrary 搜索方式。

    目录及 DLL 句柄保存在模块级列表中，确保 provider 初始化时仍然有效。
    返回值只表示运行库是否找到并完成注册，不代表 GPU 初始化已经成功；
    最终仍以三个模型的实际 session provider 为准。未安装 onnxruntime 或
    没有 nvidia-*-cu12 包时返回 False。
    """
    from pathlib import Path
    try:
        import onnxruntime
    except ImportError:
        return False
    nvidia = Path(onnxruntime.__file__).resolve().parent.parent / 'nvidia'
    bins = [str(d) for d in sorted(nvidia.glob('*/bin')) if d.is_dir()]
    if not bins:
        return False
    # 前置而非追加，确保 onnxruntime 优先找到 pip 包内的 CUDA 运行库。
    os.environ['PATH'] = os.pathsep.join(bins + [os.environ.get('PATH', '')])
    try:
        if hasattr(os, 'add_dll_directory'):
            registered = {str(h.path) for h in _CUDA_DLL_DIRECTORY_HANDLES}
            for directory in bins:
                if directory not in registered:
                    _CUDA_DLL_DIRECTORY_HANDLES.append(
                        os.add_dll_directory(directory))
        if os.name == 'nt':
            # ORT 用带 ALTERED_SEARCH_PATH 的 LoadLibrary 加载 provider;
            # 仅注册目录在本机仍会漏依赖, 先显式加载其直接依赖。
            import ctypes
            for package, names in (
                    ('cuda_runtime', ('cudart64_12.dll',)),
                    ('cublas', ('cublas64_12.dll', 'cublasLt64_12.dll')),
                    ('cufft', ('cufft64_11.dll',)),
                    ('cudnn', ('cudnn64_9.dll',))):
                for name in names:
                    _CUDA_DLL_HANDLES.append(
                        ctypes.WinDLL(str(nvidia / package / 'bin' / name)))
    except OSError as e:
        print(f'CUDA 运行库加载失败: {e}')
        return False
    return True


def _patch_rapidocr_cuda_kwargs():
    """兼容 rapidocr-onnxruntime 1.2.3 的 cls/rec CUDA 参数更新缺陷。

    原实现会给 det 参数剥去 ``det_`` 前缀，却没有同样处理 cls/rec，导致
    ``cls_use_cuda`` 和 ``rec_use_cuda`` 无法写入模型配置，方向分类与识别
    始终落到 CPU。这里按 det 的逻辑替换进程内 UpdateParameters 的两个方法，
    同时保留默认模型路径相对 rapidocr 安装目录解析的行为，不修改第三方包。
    """
    from rapidocr_onnxruntime.utils import UpdateParameters, root_dir

    def _mk(prefix):
        def _update(self, config, kw):
            if not kw:
                return config
            # 与 det 参数处理一致，先去掉 cls_/rec_ 前缀再写入模型配置。
            kw = {k.split(prefix, 1)[1]: v for k, v in kw.items()}
            if not kw.get('model_path'):
                kw['model_path'] = str(root_dir / config['model_path'])
            config.update(kw)
            return config
        return _update

    UpdateParameters.update_cls_params = _mk('cls_')
    UpdateParameters.update_rec_params = _mk('rec_')


def _engine_sessions_on_cuda(engine):
    """核实 det/cls/rec 三个实际 session 均使用 CUDAExecutionProvider。

    provider 出现在 onnxruntime 可用列表里，不代表 DLL 依赖已经成功加载；
    RapidOCR 内部对象结构不完整或 session 不可访问时也按验证失败处理，
    让构造入口明确回退 CPU，而不是把部分模型留在未知状态。
    """
    try:
        sess = [engine.text_detector.infer.session,
                engine.text_cls.infer.session,
                engine.text_recognizer.session.session]
        return all('CUDAExecutionProvider' in s.get_providers() for s in sess)
    except Exception:
        return False


def _make_ocr_engine(use_gpu=None):
    """构造 RapidOCR 实例, 识别阈值与逐格兜底的 text_score 一致 (0.3)。

    本地 (ocr_cells) 与服务端 (ocr_service.py) 共用此入口, 避免两处写死
    参数日后改不一致; 未安装 rapidocr-onnxruntime 时返回 None。

    use_gpu: True 请求使用 GPU（初始化失败仍回退 CPU），False 明确只用 CPU，
    None 自动探测（安装 onnxruntime-gpu 且 CUDA 初始化成功才保留 GPU）。GPU
    失败原因包括缺 CUDA/cuDNN DLL、驱动不匹配等；打印提示后继续 CPU 转换，
    不让可选加速能力中断文档处理。
    """
    from pathlib import Path
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    if use_gpu is None:
        # available_providers 只是安装能力提示，不能证明 CUDA DLL 可加载；
        # 因而先据此尝试 GPU，构造后再逐个检查实际 session。
        try:
            import onnxruntime as ort
            use_gpu = 'CUDAExecutionProvider' in ort.get_available_providers()
        except ImportError:
            use_gpu = False
    if use_gpu:
        _prepend_nvidia_cuda_dlls()
        _patch_rapidocr_cuda_kwargs()
        import rapidocr_onnxruntime
        models = Path(rapidocr_onnxruntime.__file__).resolve().parent / 'models'
        try:
            engine = RapidOCR(
                text_score=_OCR_TEXT_SCORE,
                det_use_cuda=True,
                det_model_path=str(models / 'ch_PP-OCRv3_det_infer.onnx'),
                cls_use_cuda=True,
                cls_model_path=str(models / 'ch_ppocr_mobile_v2.0_cls_infer.onnx'),
                rec_use_cuda=True,
                rec_model_path=str(models / 'ch_PP-OCRv3_rec_infer.onnx'))
            # 构造成功不等于 CUDA 生效，三个模型的实际 provider 必须全部核实。
            if _engine_sessions_on_cuda(engine):
                print('RapidOCR 引擎: CUDA (GPU)')
                return engine
            print('RapidOCR 引擎: CUDA 初始化失败, 回退 CPU')
        except Exception as e:
            print(f'RapidOCR 引擎: GPU 构造失败 ({e}), 回退 CPU')
    try:
        return RapidOCR(text_score=_OCR_TEXT_SCORE)
    except Exception:
        # 最后一次兜底也显式传阈值, 避免静默退回模型默认值。
        return RapidOCR(text_score=_OCR_TEXT_SCORE)
