# -*- coding: utf-8 -*-

"""数据简表、照片简表和模板匹配共用的表头列名归一规则。

这里的路由结果是数据列语义，不是格子的 label/value 等版式角色。列名去除
空白后按别名子串匹配，使“实测值”“实测结果”等写法归入同一规范角色。
"""
COLUMN_ROLE_SYNONYMS = {
    'specified': ('规定值', '标准值', '允许偏差'),
    'measured': ('实测',),
    'pass_rate': ('合格率', '通过率'),
}


def normalize_column_role(name):
    """把列名路由为 specified/measured/pass_rate，无法识别时返回 None。

    匹配前只去除空白，别名按子串而非整格精确文本匹配；返回值是下游数据
    简表与模板列之间共用的规范键。
    """
    import re
    normalized = re.sub(r'\s+', '', str(name or ''))
    # 按声明顺序匹配，首个命中的别名决定规范角色
    for role, synonyms in COLUMN_ROLE_SYNONYMS.items():
        if any(word in normalized for word in synonyms):
            return role
    # 未识别列保持未知，避免误路由到数据角色
    return None
