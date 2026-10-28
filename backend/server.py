"""表单识别与匹配服务: 简表图片识别 + 已有模板库匹配回填。

接口:
    GET  /templates                 模板库列表 (扫描 root 下所有 template.json)
    GET  /templates/{id}            加载模板 -> {template: Template}
    POST /sheet                     上传简表图片, 识别为 简化.json 同构 JSON
    POST /templates/{id}/match-sheet 上传数据简表, 按指定模板匹配回填
    POST /match-sheet               上传数据简表, 按 表名/编号 自动路由模板
                                    并匹配回填, 返回 templateUuid + values
静态:
    /        Vue 前端 (dist/, 未构建时提示)
    /files/  项目根目录 (已有模板的背景图与简表预览图, template.json 里的
             page.background 相对路径在加载时改写为 /files/...)

用法:
    .venv/Scripts/python.exe backend/server.py [--root .] [--port 8000]
前端开发模式:
    cd frontend/frontend && npm run dev   # 已配 VITE_API_BASE_URL 时直连本服务

本文件位于 backend/, 项目根目录取其上一级; 脚本入口需把项目根加进
sys.path 才能 import backend 包。
"""
import argparse
import json
import os
import sys
import threading
import time

# 项目根目录 (backend/ 的上一级), --root 默认值与 _uploads/out/dist 定位基准
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 识别/匹配管线公开函数 (叶子模块直取, 不经根目录兼容 shim)
from backend.template_pipeline.pipeline.page import extract_page
from backend.template_pipeline.semantic.fields import extract_fields
from backend.template_pipeline.semantic.index import build_index
from backend.template_pipeline.semantic.match import match_data_fields
from backend.template_pipeline.semantic.roles import assign_roles
from backend.template_pipeline.semantic.sheet_input import photo_to_sheet
from backend.template_pipeline.semantic.simplified_sheet import sheet_to_simplified

# 扫描模板时跳过依赖、缓存与非模板生成目录 (与 .gitignore 对齐)
_SKIP_DIRS = {'.git', '.venv', 'node_modules', '_tmp_form', '_uploads',
              '_sandbox', 'dist', '__pycache__'}
_IMG_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.webp')
_SCAN_TTL = 2.0

# 模板注册表是进程级只读快照；扫描线程先构建新表，再在锁内整体替换，
# 避免请求线程看到扫描到一半的注册表。
_registry = {}  # templateUuid -> template.json 绝对路径
_registry_lock = threading.RLock()
_registry_scanned_at = 0.0


def _index_score(template_path):
    """返回同 UUID 模板产物的完整性评分。

    评分依次比较数据行、上下文字段和通用标签的数量；缺失或损坏的
    form.json 按空索引处理，不阻断其他模板的扫描。
    """
    try:
        with open(os.path.join(os.path.dirname(template_path), 'form.json'),
                  encoding='utf-8') as f:
            index = json.load(f).get('index') or {}
        return (len(index.get('rows') or []),
                len(index.get('header_fields') or {}),
                len(index.get('by_label') or {}))
    except Exception:
        return (0, 0, 0)


def scan_templates(root):
    """扫描 root 下的模板并刷新注册表，两次完整扫描至少间隔 2 秒。

    每个模板目录必须包含 template.json；form.json 仅用于评估同 UUID
    候选的索引完整性。损坏的单个模板会被跳过，不影响其余模板。
    """
    global _registry, _registry_scanned_at
    with _registry_lock:
        now = time.monotonic()
        if now - _registry_scanned_at < _SCAN_TTL:
            return
        new_registry = {}
        new_scores = {}
        for dirpath, dirnames, filenames in os.walk(root):
            # 原地裁剪 dirnames 才会阻止 os.walk 继续进入依赖和缓存目录。
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith('.')]
            if 'template.json' not in filenames:
                continue
            path = os.path.join(dirpath, 'template.json')
            try:
                with open(path, encoding='utf-8') as f:
                    tpl = json.load(f)
                tid = tpl['templateUuid']
                # 空壳 index 与完整索引不能同权：比较可用条目数，
                # 同 UUID 的多份产物中保留回填信息最完整的一份。
                score = _index_score(path)
                if tid not in new_registry or score > new_scores[tid]:
                    new_registry[tid] = path
                    new_scores[tid] = score
            except Exception as e:
                print(f'[scan] 跳过 {path}: {e}')
        # 一次性替换完整快照，请求不会读到只扫描了一半的注册表。
        _registry = new_registry
        _registry_scanned_at = time.monotonic()


def bg_to_url(tpl, path, root):
    """将模板各页的相对背景图路径改为 /files/... URL。

    绝对 URL 和已经以斜杠开头的服务路径保持不变；返回原模板字典，
    调用方得到的是已就地更新的页数据。
    """
    base = '/files/' + os.path.dirname(os.path.relpath(path, root)) \
        .replace(os.sep, '/')
    for page in tpl.get('pages') or []:
        bg = page.get('background') or ''
        if bg and not bg.startswith('/') and not bg.startswith('http'):
            page['background'] = base + '/' + bg
    return tpl


def _convert_args():
    """图片识别参数的集中默认值。"""
    import argparse
    return argparse.Namespace(
        page=0, dpi=300, h_ratio=0.1, v_ratio=0.03, h_min_span=0.28,
        v_min_span=0.04, h_span_ratio=0.45, v_span_ratio=0.20,
        no_ocr=False, out=None)


def _route_template(sheet):
    """按 表名/编号 给注册表里的模板打分, 返回 (tid, score) 最高者。

    表名完全相等 +10 / 互相包含 +5; 编号 (从 '编 号:JB12-4.5.2-4'
    类文本提取代码) 相等 +10 / 前缀家族 +3。编号在数据简表上是逐记录
    流水, 与模板印刷编号不一定全等, 所以表名是主键、编号是辅键。"""
    import re

    def norm(s):
        # 比较前统一括号并移除空白，吸收 OCR 与模板录入的格式差异。
        s = str(s or '').translate({ord('（'): '(', ord('）'): ')'})
        return re.sub(r'\s+', '', s)

    def code_of(text):
        m = re.search(r'编号\s*[::]\s*([A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*)',
                      str(text or ''))
        return m.group(1) if m else None

    title = norm(sheet.get('title'))
    sheet_code = code_of((sheet.get('context') or {}).get('编号'))
    best = None
    for tid, path in _registry.items():
        try:
            with open(os.path.join(os.path.dirname(path), 'form.json'),
                      encoding='utf-8') as f:
                form = json.load(f)
        except Exception:
            continue
        score = 0
        t_title = norm((form.get('form') or {}).get('meta', {}).get('title'))
        if title and t_title:
            if title == t_title:
                score += 10
            elif len(title) >= 4 and (title in t_title or t_title in title):
                score += 5
        if sheet_code:
            idx = form.get('index') or {}
            hf = idx.get('header_fields') or {}
            num_u = hf.get('编号')
            num_u = num_u.get('cell') if isinstance(num_u, dict) else num_u
            t_code = code_of(((idx.get('cells') or {}).get(num_u) or {})
                             .get('text'))
            if t_code:
                if norm(sheet_code) == norm(t_code):
                    score += 10
                elif t_code.startswith(sheet_code) \
                        or sheet_code.startswith(t_code):
                    score += 3
        if best is None or score > best[1]:
            # 同分保留先扫描到的模板；是否可用由调用方检查最高分是否大于 0。
            best = (tid, score)
    return best


def _safe_upload_name(filename):
    """返回不含路径信息的 (stem, 小写扩展名), 非法文件名直接拒绝。"""
    from fastapi import HTTPException
    raw = filename or ''
    if ('/' in raw or '\\' in raw or '..' in raw
            or any(ord(c) < 32 or c in '<>:"|?*' for c in raw)):
        raise HTTPException(400, '文件名包含路径分隔符、.. 或非法字符')
    stem, ext = os.path.splitext(os.path.basename(raw))
    stem = stem.strip()
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if stem.upper() in reserved or stem.endswith(('.', ' ')):
        raise HTTPException(400, '文件名不是合法的 Windows 文件名')
    return stem or 'upload', ext.lower()


def _remove_file(path):
    """尽力删除请求暂存文件；文件已不存在或清理失败时不覆盖业务结果。"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _parse_sheet_file(tmp, ext):
    """在线程池中执行完整识别管线，将暂存图片转换为内部简表。

    几何检测、OCR、角色标注、字段抽取和索引构建均在这里串行完成；
    本函数不读取模板，也不做字段匹配。
    """
    from fastapi import HTTPException
    data = extract_page(tmp, 0, _convert_args())
    if not data or not data.get('cells'):
        raise HTTPException(400, '照片中未检测到表格, 请拍正拍清晰后重试')
    assign_roles(data)
    extract_fields(data)
    build_index(data)
    sheet = photo_to_sheet(data)
    print(f"[match-sheet] 照片简表: title={sheet['title']} "
          f"fields={len(sheet['fields'])}")
    if not sheet['fields']:
        raise HTTPException(400,
                            '未能从简表解析出字段 '
                            '(需要 项目/规定值/实测值 类表头)')
    return sheet


async def _parse_sheet_upload(file, upload_dir):
    """校验上传图片并暂存，在线程池识别后返回 ``(sheet, 暂存路径)``。

    暂存文件使用随机 UUID 名称，避免不同请求互相覆盖；识别失败时立即
    清理，成功时交由路由的 finally 块统一清理。
    """
    import uuid
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool
    _, ext = _safe_upload_name(file.filename)
    if ext not in _IMG_EXT:
        raise HTTPException(400, '仅支持已填表简表图片')
    os.makedirs(upload_dir, exist_ok=True)
    tmp = os.path.join(upload_dir, f'sheet_{uuid.uuid4().hex}{ext}')
    try:
        with open(tmp, 'wb') as f:
            f.write(await file.read())
        # 几何检测和 OCR 均为阻塞型 CPU 操作，放入线程池避免卡住事件循环。
        sheet = await run_in_threadpool(_parse_sheet_file, tmp, ext)
        return sheet, tmp
    except Exception:
        _remove_file(tmp)
        raise


def _sheet_preview_url(path, upload_dir):
    """按内容哈希暂存上传图片, 返回 /files/... 预览 URL 或 None。"""
    import hashlib
    ext = os.path.splitext(path)[1].lower()
    try:
        # 哈希仅用于相同图片的预览去重，不承担安全校验用途。
        digest = hashlib.md5(open(path, 'rb').read()).hexdigest()[:8]
        dst = os.path.join(upload_dir, f'sheet_preview_{digest}{ext}')
        if not os.path.exists(dst):
            import shutil
            shutil.copy(path, dst)
        rel = os.path.relpath(dst, os.path.dirname(upload_dir)) \
            .replace(os.sep, '/')
        return '/files/' + rel
    except Exception as e:
        print(f'[sheet-preview] 生成失败: {e}')
        return None


def create_app(root=ROOT):
    """创建识别与匹配服务，所有模板、预览和静态资源均相对 root 定位。

    CPU/图像处理及磁盘扫描通过线程池执行，避免阻塞 FastAPI 事件循环。
    """
    from fastapi import FastAPI, File, HTTPException, Request, UploadFile
    from fastapi.concurrency import run_in_threadpool
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    root = os.path.abspath(root)
    dist = os.path.join(root, 'frontend', 'frontend', 'dist')
    upload_dir = os.path.join(root, '_uploads')

    app = FastAPI(title='表单识别与匹配服务')
    app.add_middleware(CORSMiddleware, allow_origins=['*'],
                       allow_methods=['*'], allow_headers=['*'])

    @app.middleware('http')
    async def protect_files(request: Request, call_next):
        """静态目录仅放行普通产物, 拒绝敏感目录、点文件与路径穿越。"""
        path = request.url.path
        if path == '/files' or path.startswith('/files/'):
            parts = path.replace('\\', '/').split('/')[2:]
            # _uploads 虽不参与模板扫描, 但简表预览图需要正常访问。
            denied = {d.casefold() for d in (_SKIP_DIRS - {'_uploads'}) | {'deploy'}}
            if any(p.startswith('.') or p.casefold() in denied for p in parts):
                return JSONResponse({'detail': '禁止访问该路径'}, status_code=403)
        return await call_next(request)

    @app.get('/templates')
    def list_templates():
        """返回当前注册表的模板摘要，不返回体积较大的完整页数据。"""
        scan_templates(root)
        summaries = []
        for tid, path in _registry.items():
            try:
                with open(path, encoding='utf-8') as f:
                    tpl = json.load(f)
                summaries.append({
                    'templateUuid': tid,
                    'name': tpl.get('name'),
                    'sourceFormat': tpl.get('sourceFormat'),
                    'pageCount': len(tpl.get('pages') or []),
                })
            except Exception:
                continue
        return summaries

    @app.get('/templates/{tid}')
    def load_template(tid: str):
        """读取指定模板，并将页背景图路径转换为可访问的静态 URL。"""
        scan_templates(root)
        path = _registry.get(tid)
        if not path:
            raise HTTPException(404, '模板不存在')
        with open(path, encoding='utf-8') as f:
            return {'template': bg_to_url(json.load(f), path, root)}

    @app.post('/sheet')
    async def sheet_from_photo(file: UploadFile = File(...)):
        """上传一张简表图片, 返回与 简化.json 同构的 JSON。"""
        _, ext = _safe_upload_name(file.filename)
        if ext not in _IMG_EXT:
            raise HTTPException(
                400, f'仅支持图片简表 ({"/".join(_IMG_EXT)})')
        tmp = None
        try:
            sheet, tmp = await _parse_sheet_upload(file, upload_dir)
            return sheet_to_simplified(sheet)
        finally:
            _remove_file(tmp)

    @app.post('/templates/{tid}/match-sheet')
    async def match_sheet(tid: str, file: UploadFile = File(...)):
        """上传简表图片, 解析成一次性 JSON 并按指定模板索引匹配,
        返回 {title, fields, values: {fieldUuid: 值}, unmatched}。
        简表暂存文件在响应前清理, 内容哈希预览图可保留。"""
        tmp = None
        try:
            await run_in_threadpool(scan_templates, root)
            path = _registry.get(tid)
            if not path:
                raise HTTPException(404, '模板不存在')
            sheet, tmp = await _parse_sheet_upload(file, upload_dir)
            with open(os.path.join(os.path.dirname(path), 'form.json'),
                      encoding='utf-8') as f:
                index = (json.load(f).get('index')) or {}
            res = await run_in_threadpool(match_data_fields, sheet, index)
            print(f"[match-sheet] title={sheet['title']} "
                  f"fields={len(sheet['fields'])} "
                  f"命中={len(res['values'])} 未匹配={res['unmatched']}")
            preview = await run_in_threadpool(
                _sheet_preview_url, tmp, upload_dir)
            return {**sheet, **res, 'sheetImage': preview}
        finally:
            _remove_file(tmp)

    @app.post('/match-sheet')
    async def match_sheet_auto(file: UploadFile = File(...)):
        """上传数据简表: 解析后按 表名/编号 自动路由模板库并匹配回填,
        返回 {templateUuid, title, context, fields, values, unmatched}。"""
        tmp = None
        try:
            sheet, tmp = await _parse_sheet_upload(file, upload_dir)
            await run_in_threadpool(scan_templates, root)
            routed = await run_in_threadpool(_route_template, sheet)
            if not routed or routed[1] <= 0:
                raise HTTPException(
                    404, f"没有匹配到模板 (简表表名: {sheet['title'] or '未识别'}, "
                         f"请确认模板库中已有对应模板)")
            tid = routed[0]
            with open(os.path.join(os.path.dirname(_registry[tid]), 'form.json'),
                      encoding='utf-8') as f:
                index = (json.load(f).get('index')) or {}
            res = await run_in_threadpool(match_data_fields, sheet, index)
            print(f"[match-sheet:auto] title={sheet['title']} -> {tid} "
                  f"score={routed[1]} 命中={len(res['values'])} "
                  f"未匹配={res['unmatched']}")
            preview = await run_in_threadpool(
                _sheet_preview_url, tmp, upload_dir)
            return {'templateUuid': tid, **sheet, **res, 'sheetImage': preview}
        finally:
            _remove_file(tmp)

    # /files: 项目根 (已有模板的背景图与简表预览图)
    app.mount('/files', StaticFiles(directory=root), name='files')

    # Vue 前端 SPA (dist 不存在时给出提示)
    @app.get('/{full_path:path}')
    def spa(full_path: str):
        """优先返回前端静态文件，其他路径回退到 SPA 的 index.html。"""
        candidate = os.path.join(dist, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        index = os.path.join(dist, 'index.html')
        if os.path.isfile(index):
            return FileResponse(index)
        raise HTTPException(
            404, '前端未构建: 先运行 cd frontend/frontend && npm run build')

    return app


app = create_app()

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='表单识别与匹配服务')
    ap.add_argument('--root', default=ROOT, help='项目根目录 (默认脚本所在)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8000)
    cli = ap.parse_args()
    app = create_app(os.path.abspath(cli.root))
    import uvicorn
    # Windows 控制台/重定向默认 GBK, 识别文字可能含 GBK 无法编码的字符,
    # 打印抛 UnicodeEncodeError 会中断请求; 重配 stdout/stderr 为 UTF-8 容错输出
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='backslashreplace')
        except Exception:
            pass
    print(f'表单识别与匹配服务: http://{cli.host}:{cli.port}')
    print(f'项目根: {os.path.abspath(cli.root)}')
    print('前端开发: cd frontend/frontend && npm run dev')
    uvicorn.run(app, host=cli.host, port=cli.port)
