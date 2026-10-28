# -*- coding: utf-8 -*-

# 通用简表 -> 对外简化 JSON。只转换结构，不参与 OCR 或模板匹配。

import re


# 对外字段 -> 内部 context 可接受的标签，按声明顺序取值；别名仅适配
# 不同表格的命名差异，不在此处猜测或合成业务值。
_CONTEXT_LABELS = {
    '项目编号': ('项目编号',),
    '项目名称': ('项目名称',),
    '表格类型': ('编号', '表格类型'),
    '开工类型': ('开工类型',),
    '监理单位标段': ('监理单位标段', '监理标段'),
    '监理单位名称': ('监理单位名称', '监理单位'),
    '施工单位标段': ('施工单位标段', '施工标段'),
    '施工单位名称': ('施工单位名称', '施工单位'),
    '抽检人': ('抽检人', '抽检'),
    '计划开始日期': ('抽检时间', '计划开始日期', '抽检日期'),
    '工程名称': ('工程部位', '分部工程', '工程名称'),
    '前桩号': ('前桩号',),
    '后桩号': ('后桩号',),
}


def normalize_field_name(text):
    """去除空白、统一中英文括号，生成字段匹配名。"""
    return re.sub(r'\s+', '', str(text or '')).translate(
        {ord('（'): '(', ord('）'): ')'})


def _context_value(context, *labels):
    """按归一化标签查找上下文，空字符串视为缺值。"""
    normalized = {normalize_field_name(k): v for k, v in context.items()}
    for label in labels:
        value = normalized.get(normalize_field_name(label))
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _simplified_field(field):
    """补齐对外字段键并复制识别值；measured 始终输出为独立列表。"""
    # 兼容内部字段的 name/key 两种命名，优先保留业务字段名称。
    key = field.get('name') or field.get('key')
    measured = field.get('measured')
    return {
        'key': key,
        'norm': normalize_field_name(key),
        'specified': field.get('specified'),
        # 复制实测值列表，避免调用方修改输出时反向改动原简表。
        'measured': list(measured) if isinstance(measured, list) else [],
        'pass_rate': field.get('pass_rate'),
    }


def sheet_to_simplified(sheet):
    """将内部简表转换为稳定的对外“简化 JSON”字典。

    顶部业务字段从 context 的标签别名中取值，缺失统一为 None；表格名称
    来自 title，分部工程保持嵌套结构。fields 保留输入顺序，并统一为
    key/norm/specified/measured/pass_rate 五项。

    本函数只投影和复制数据，不推断缺值、不执行 OCR 或模板匹配，也不修改
    输入简表。
    """
    context = sheet.get('context') or {}
    # 显式列出对外键，固定 API 结构并避免内部临时字段泄漏；未识别项保持
    # None，由调用方决定如何展示或校验。
    result = {
        '项目编号': _context_value(context, *_CONTEXT_LABELS['项目编号']),
        '项目名称': _context_value(context, *_CONTEXT_LABELS['项目名称']),
        '表格类型': _context_value(context, *_CONTEXT_LABELS['表格类型']),
        '表格名称': sheet.get('title') or None,
        '开工类型': _context_value(context, *_CONTEXT_LABELS['开工类型']),
        '监理单位标段': _context_value(
            context, *_CONTEXT_LABELS['监理单位标段']),
        '监理单位名称': _context_value(
            context, *_CONTEXT_LABELS['监理单位名称']),
        '施工单位标段': _context_value(
            context, *_CONTEXT_LABELS['施工单位标段']),
        '施工单位名称': _context_value(
            context, *_CONTEXT_LABELS['施工单位名称']),
        '抽检人': _context_value(context, *_CONTEXT_LABELS['抽检人']),
        '计划开始日期': _context_value(
            context, *_CONTEXT_LABELS['计划开始日期']),
        '分部工程': {
            '工程名称': _context_value(context, *_CONTEXT_LABELS['工程名称']),
            '前桩号': _context_value(context, *_CONTEXT_LABELS['前桩号']),
            '后桩号': _context_value(context, *_CONTEXT_LABELS['后桩号']),
        },
        'fields': [_simplified_field(f) for f in sheet.get('fields') or []],
    }
    return result
