"""
fastgpt 推送模块：调用 /api/core/dataset/data/pushData 接口将 (q, a) 写入知识库。

- 带重试（复用 retry.run_with_retry），超时时间由 config.FASTGPT_TIMEOUT 配置，
  并支持命令行 --timeout 覆盖；
- 响应校验 code == 200（兼容部分版本返回 null/0）作为成功标识。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 便于以脚本方式运行时也能 import 父级 find_similar_jira 的 utils 模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

import config as cfg
from utils.logger import log as mylog
from retry import run_with_retry


def _do_push(json_q: str, json_a: str) -> dict:
    """执行一次 pushData POST 请求；失败抛 requests 异常，由重试机制处理。"""
    headers = {
        "Authorization": f"Bearer {cfg.FASTGPT_TOKEN}",
        "Content-Type": "application/json",
    }
    body: dict = {
        "collectionId": cfg.COLLECTION_ID,
        "trainingType": cfg.TRAINING_TYPE,
        "data": [{"q": json_q, "a": json_a}],
    }
    if cfg.BILL_ID:
        body["billId"] = cfg.BILL_ID

    mylog(
        f"[推送] {cfg.FASTGPT_TIMEOUT}秒超时下 POST {cfg.FASTGPT_URL}，"
        f"发送 q({len(json_q)}字符)/a({len(json_a)}字符)，collectionId={cfg.COLLECTION_ID}"
    )
    resp = requests.post(
        cfg.FASTGPT_URL,
        headers=headers,
        json=body,
        timeout=cfg.FASTGPT_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    code = result.get("code")
    # fastgpt 成功时 code 通常为 200；部分版本可能返回 null/0
    if code not in (None, 0, 200):
        raise ValueError(f"fastgpt 返回异常状态：code={code}，statusText={result.get('statusText')}")
    mylog(f"[推送] 请求成功：code={code}，响应摘要：{str(result)[:200]}")
    return result


def push_to_fastgpt(
    json_q: str,
    json_a: str,
    retries: int | None = None,
    interval: float | None = None,
) -> dict:
    """向 fastgpt 知识库推送一条 (q, a)。

    参数：
        json_q:   问题文本（LLM 提炼的问题总结）。
        json_a:   答案文本（完整 filter_a_json 的 JSON 字符串）。
        retries:  最大重试次数，默认读 config.RETRY_TIMES。
        interval: 重试间隔（秒），默认读 config.RETRY_INTERVAL。
    返回：
        fastgpt 响应 dict。
    抛出：
        多次重试后仍失败抛 RetryFailedError（调用方记录为失败，不入库）。
    """
    return run_with_retry(
        desc="推送数据到 fastgpt pushData",
        func=_do_push,
        retries=retries if retries is not None else cfg.RETRY_TIMES,
        interval=interval if interval is not None else cfg.RETRY_INTERVAL,
        retry_exceptions=(Exception,),
        json_q=json_q,
        json_a=json_a,
    )