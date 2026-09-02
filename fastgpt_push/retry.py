"""
通用重试工具模块。

覆盖范围：
- 网络请求（jira 读取、fastgpt 推送、tokenize 接口）；
- LLM 调用（langchain invoke 底层网络）；
- JSON 解析（如 root_cause 提取结果的 json.loads）。

规则：超过最大重试次数仍失败即抛 RetryFailedError，由上层决定该条 JIRA 不入库。
每次失败都会打印具体错误内容，不使用"处理中"这类抽象文案。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

# 便于以脚本方式运行时也能 import 父级 find_similar_jira 的 utils / src 模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import log as mylog


class RetryFailedError(Exception):
    """多次重试后仍失败时抛出的异常，携带环节描述与每次失败原因。"""

    def __init__(self, desc: str, errors: list[str]):
        self.desc = desc
        self.errors = errors
        joined = "；".join(errors)
        super().__init__(f"[{desc}] 重试后仍失败：{joined}")

    def format_error_chain(self) -> str:
        """按行返回完整失败原因链，供 main 的失败明细块使用。"""
        lines = [f"环节：{self.desc}"]
        lines.extend(f"  第{i}次失败原因：{err}" for i, err in enumerate(self.errors, start=1))
        return "\n".join(lines)


def run_with_retry(
    desc: str,
    func: Callable[..., Any],
    retries: int = 3,
    interval: float = 2.0,
    retry_exceptions: tuple = (Exception,),
    *args,
    **kwargs,
) -> Any:
    """执行 func(*args, **kwargs)，失败时按 retry_exceptions 捕获并重试。

    参数：
        desc:              环节描述，用于具体日志，例如"获取 JIRA OTT-80575 的评论"。
        func:              要执行的操作。
        retries:           最大重试次数（含首次）。
        interval:          每次重试前的等待秒数。
        retry_exceptions:  需要捕获并重试的异常类型元组。
    返回：
        func 的成功返回值。
    抛出：
        最终失败时抛 RetryFailedError（聚合每次失败原因，由调用方决定本条不入库）。
    """
    errors: list[str] = []
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except retry_exceptions as exc:
            message = f"{type(exc).__name__}: {exc}"
            mylog(f"[{desc}] 第 {attempt}/{retries} 次失败：{message}")
            errors.append(message)
            if attempt < retries:
                mylog(f"[{desc}] 等待 {interval} 秒后重试...")
                time.sleep(interval)
    raise RetryFailedError(desc, errors)


def parse_json_with_retry(
    desc: str,
    text: Any,
    retries: int = 3,
    interval: float = 2.0,
) -> Any:
    """解析 JSON 文本，解析失败时重试直至成功或耗尽次数。

    参数：
        desc:      环节描述，用于具体日志。
        text:      待解析内容；若已是 dict/list 则直接原样返回。
        retries:   最大重试次数（含首次）。
        interval:  每次重试前的等待秒数。
    返回：
        解析后的对象。
    抛出：
        最终失败时抛 RetryFailedError。
    """
    if isinstance(text, (dict, list)):
        return text
    errors: list[str] = []
    for attempt in range(1, retries + 1):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            snippet = str(text)[:120]
            message = f"{type(exc).__name__}: {exc}；文本片段：{snippet}"
            mylog(f"[{desc}] 第 {attempt}/{retries} 次解析失败：{message}")
            errors.append(message)
            if attempt < retries:
                mylog(f"[{desc}] 等待 {interval} 秒后重试...")
                time.sleep(interval)
    raise RetryFailedError(desc, errors)