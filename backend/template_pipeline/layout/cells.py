# -*- coding: utf-8 -*-
"""由横竖线位置和像素掩码重建表格单元格。

h_pos、v_pos 为升序的整页线坐标，horiz、vert 为与原图同尺寸的单通道
前景掩码。输出的 row、col 是原子网格左上角索引，x、y、w、h 则始终保持
整页像素坐标，供版面分析与标准数据组装共同使用。
"""

# ---------------------------------------------------------------------------
# 交点与格子
# ---------------------------------------------------------------------------


def compute_intersections(h_pos, v_pos, horiz, vert, win=5):
    """计算横竖线候选交点，返回 [{'x': x, 'y': y}, ...]。

    候选来自横、竖线坐标的笛卡尔积。线心可能有少量像素偏差，因此只要求
    同一邻域内两个方向的掩码均有前景，而不要求它们在理论上精确相交；
    这里不做整条边的连续性检查。
    """
    points = []
    for y in h_pos:
        for x in v_pos:
            # 交点允许有 win 像素偏差，切片边界需限制在图像范围内。
            y0, y1 = max(0, y - win), min(horiz.shape[0], y + win + 1)
            x0, x1 = max(0, x - win), min(horiz.shape[1], x + win + 1)
            # 两个方向的掩码在邻域内都有前景，才接受该候选交点。
            if horiz[y0:y1, x0:x1].any() and vert[y0:y1, x0:x1].any():
                points.append({'x': x, 'y': y})
    return points


def _edge_present(mask, p1, p2, axis, thickness=2, density=0.4):
    """检查 p1->p2 之间是否存在足够连续的线段。

    以两端中点为中心截取窄带，再将窄带归约为沿边延伸方向的投影。density
    表示投影中有像素的位置占比，而非窄带面积占比，因此能容忍线宽变化与
    局部小断口，同时排除只在端点附近有墨迹的伪边。
    """
    if axis == 'h':
        # 横边使用两端的平均 y 坐标，裁出覆盖线宽的竖向窄带。
        y = (p1[1] + p2[1]) // 2
        band = mask[max(0, y - thickness):y + thickness + 1,
                    min(p1[0], p2[0]):max(p1[0], p2[0]) + 1]
    else:
        # 竖边同理，沿 y 方向截取两端之间的区域。
        x = (p1[0] + p2[0]) // 2
        band = mask[min(p1[1], p2[1]):max(p1[1], p2[1]) + 1,
                    max(0, x - thickness):x + thickness + 1]
    if band.size == 0:
        return False
    # 窄带任一行/列有像素即表示该位置有线，再统计沿线方向的有效覆盖率。
    proj = band.any(axis=0 if axis == 'h' else 1)
    return proj.mean() >= density


# 公开给整页数据模型使用；私有名保留给既有调用方。
edge_present = _edge_present


def find_cells(h_pos, v_pos, horiz, vert):
    """由相邻线围成的原子块重建小格子（含合并单元格）。

    相邻原子块按“公共边是否缺线”做并查集合并：没有分隔线的块属于同一个
    合并格。只保留至少一个原子块具有两条及以上边的分组，以丢弃表格外
    开放区域。输出按 row、col 升序排列；每项含左上角原子网格索引、
    rowspan/colspan，以及覆盖整个合并区域的整页外接矩形 x、y、w、h。
    """
    # N 条线只能围成 N-1 个原子区域，先确定原子网格的行列数。
    R, C = len(h_pos) - 1, len(v_pos) - 1
    if R <= 0 or C <= 0:
        return []

    def tid(i, j):
        # 将二维原子格编号压缩为并查集的一维下标。
        return i * C + j

    # parent 初始各自指向自己，即每个原子格先作为独立集合。
    parent = list(range(R * C))

    def find(a):
        while parent[a] != a:
            # 路径压缩：查找根节点时顺带缩短后续查询路径。
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def edges(i, j):
        tl = (v_pos[j], h_pos[i])
        tr = (v_pos[j + 1], h_pos[i])
        bl = (v_pos[j], h_pos[i + 1])
        br = (v_pos[j + 1], h_pos[i + 1])
        return (
            _edge_present(horiz, tl, tr, 'h'),  # top
            _edge_present(horiz, bl, br, 'h'),  # bottom
            _edge_present(vert, tl, bl, 'v'),   # left
            _edge_present(vert, tr, br, 'v'),   # right
        )

    tiles = {}
    for i in range(R):
        for j in range(C):
            # 缓存四边检测结果，后续过滤开放区域时无需重复访问像素。
            e = edges(i, j)
            tiles[tid(i, j)] = e
            if j + 1 < C and not e[3]:      # 右边没分隔线 -> 与右邻同属一格
                union(tid(i, j), tid(i, j + 1))
            if i + 1 < R and not e[1]:      # 下边没分隔线 -> 与下邻同属一格
                union(tid(i, j), tid(i + 1, j))

    groups = {}
    for i in range(R):
        for j in range(C):
            # 按并查集根节点聚合，得到每个候选合并格的原子块列表。
            groups.setdefault(find(tid(i, j)), []).append((i, j))

    cells = []
    for members in groups.values():
        if max(sum(tiles[tid(i, j)]) for i, j in members) < 2:
            continue  # 表格外的开放区域
        is_ = [m[0] for m in members]
        js = [m[1] for m in members]
        i0, i1, j0, j1 = min(is_), max(is_), min(js), max(js)
        # 外接矩形从左上原子块的边界延伸到右下原子块之后的网格线。
        x, y = v_pos[j0], h_pos[i0]
        cells.append({
            'row': i0, 'col': j0,
            'rowspan': i1 - i0 + 1, 'colspan': j1 - j0 + 1,
            'x': x, 'y': y,
            'w': v_pos[j1 + 1] - x, 'h': h_pos[i1 + 1] - y,
        })
    cells.sort(key=lambda c: (c['row'], c['col']))
    return cells
