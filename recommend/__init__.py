"""推荐引擎包（寻味美食推荐平台）。

分层与职责（与 `docs/CONTRACTS.md` 第 3 节严格对齐）：

- `recommend.hot`     热度分：互动量加权 + 时间衰减，批量回写 ``Recipe.hot_score``
- `recommend.content` 内容画像：用户偏好 → ``ContentProfile``，给单菜谱打 0~1 分并产出中文理由
- `recommend.cf`      item-based 协同过滤：用户-物品稀疏矩阵 + 物品相似度缓存（纯 dict/numpy）
- `recommend.hybrid`  混合排序：内容 / CF / 热度 / 新鲜度加权，MMR 去同质化，场景过滤，异常兜底

对外统一入口：``recommend.hybrid.recommend_for_user`` / ``recommend.hybrid.recommend_similar``。
其余模块只依赖 ``food.models``，不反向依赖 API 层。
"""
from __future__ import annotations

__all__ = ["hot", "content", "cf", "hybrid"]
