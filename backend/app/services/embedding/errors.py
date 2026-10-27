"""Embedding 相关异常。

区分两类失败：
- `FatalModelError`：配置/模型不匹配（比如维度对不上），重试无用，必须 fail-fast，
  不能被 auto 模式降级成 hash 伪向量吞掉 —— 那样库里的向量全是垃圾却一片绿灯。
- 普通 `Exception`：环境问题（文件下载失败、内存不足），可以降级。
"""

from __future__ import annotations


class FatalModelError(RuntimeError):
    """模型与配置不匹配，必须人工修正后才能继续。"""
