# -*- coding: utf-8 -*-

"""按纯几何和字号规则标注文字行角色，不依赖 OCR 或具体文档内容。

输入行需带有 ``size``（字号）和 ``cx``（水平中心）；分类结果直接写回
每行的 ``role`` 字段，供后续版式还原区分标题、小标题和正文。
"""


def _classify_line_roles(lines, page_w):
    """按正文字号基准和页面相对位置，就地写入 title/heading/body。

    字号中位数作为正文基准，避免少量大标题把平均值拉高。字号至少为正文
    1.3 倍且中心距页面中线小于页宽 12% 的行判为标题；同字号但不居中的行
    判为小标题。1.12～1.3 倍之间的行一律作为小标题，其余作为正文。

    使用页宽比例而非固定像素居中容差，使同一规则适用于不同扫描尺寸。
    空列表直接返回；其余情况下原地修改 ``lines``，不另建结果列表。
    """
    if not lines:
        return
    sizes = sorted(l['size'] for l in lines)
    # 中位数代表普通正文，少量大标题不会显著抬高该基准。
    body = sizes[len(sizes) // 2]
    for l in lines:
        # 居中容差随页宽缩放，避免不同扫描分辨率下使用固定像素阈值。
        centered = abs(l['cx'] - page_w / 2) < page_w * 0.12
        if l['size'] >= body * 1.3:
            l['role'] = 'title' if centered else 'heading'
        elif l['size'] >= body * 1.12:
            l['role'] = 'heading'
        else:
            l['role'] = 'body'
