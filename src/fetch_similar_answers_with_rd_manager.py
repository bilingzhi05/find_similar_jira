"""
获取 fastgpt 相似答案，并为每条相似 jira 解析出唯一的 RD manager。

RD manager 的解析逻辑已抽离到 src.jira_manager（完全自包含），
本文件只负责 fastgpt 调用、结果解析与主流程编排。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# 兼容直接以脚本方式运行（python src/fetch_...py）时也能 import src 包
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jira_manager import (
    DEFAULT_MANAGER,
    HTTP_RETRY_INTERVAL,
    HTTP_RETRY_TIMES,
    _log,
    create_jira_client,
    get_manager_by_jira_id,
)


# --------------------------------------------------------------------------- #
# fastgpt 配置
# --------------------------------------------------------------------------- #
FASTGPT_TIMEOUT = 60        # fastgpt 请求超时（秒）

# fastgpt 默认 token（与 processor.py 保持一致）
FASTGPT_DEFAULT_TOKEN = "fastgpt-hDRWg3sKXu2mdZaTvXmfv1kaL6uAMxkrQO9oTRpm60WefI200D4Ra27C"


# --------------------------------------------------------------------------- #
# fastgpt 相关
# --------------------------------------------------------------------------- #
def fastgpt_chat_completion(
    content: str,
    access_token: str | None = None,
    uid: str = "qwertyuio-123456",
    name: str = "zhansan",
    url: str = "http://10.18.11.98:3000/api/v1/chat/completions",
) -> str:
    """调用 fastgpt 接口，返回模型回复文本；失败/超时重试后仍失败返回空字符串。"""
    token = access_token or os.getenv("FASTGPT_API_KEY") or FASTGPT_DEFAULT_TOKEN
    if not token:
        _log("[fastgpt] 未提供 FASTGPT_API_KEY，返回空结果")
        return ""

    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "stream": False,
        "detail": False,
        "variables": {"uid": uid, "name": name},
        "messages": [{"role": "user", "content": content}],
    }

    for attempt in range(1, HTTP_RETRY_TIMES + 1):
        try:
            _log(f"[fastgpt] 第 {attempt}/{HTTP_RETRY_TIMES} 次请求...")
            response = requests.post(url, headers=headers, json=payload, timeout=FASTGPT_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout as e:
            _log(f"[fastgpt] 第 {attempt} 次请求超时: {e}")
        except requests.exceptions.RequestException as e:
            _log(f"[fastgpt] 第 {attempt} 次请求失败: {e}")
        except (KeyError, IndexError, TypeError, ValueError) as e:
            _log(f"[fastgpt] 第 {attempt} 次响应解析失败: {e}")
        if attempt < HTTP_RETRY_TIMES:
            _log(f"[fastgpt] {HTTP_RETRY_INTERVAL} 秒后重试...")
            time.sleep(HTTP_RETRY_INTERVAL)

    _log("[fastgpt] 多次重试后仍失败，返回空结果")
    return ""


def parse_similar_answers(raw_text: str) -> list[dict]:
    """解析 fastgpt 返回文本中的相似 answer，返回 dict 列表；异常时返回空列表。"""
    raw_text = str(raw_text or "")
    pattern = r'相似的answer有：([\s\S]*)'
    answer_match = re.search(pattern, raw_text, re.S)
    if not answer_match:
        return []
    ans_list_str = answer_match.group(1).strip()

    # 外层通常是 list[str]，每个元素又是一个 JSON 字符串
    try:
        outer_list = json.loads(ans_list_str)
    except (json.JSONDecodeError, ValueError) as e:
        _log(f"[parse] 外层 JSON 解析失败: {e}")
        # 兼容直接返回单个对象的情况
        try:
            single = json.loads(ans_list_str)
        except (json.JSONDecodeError, ValueError) as e2:
            _log(f"[parse] 单对象 JSON 解析也失败: {e2}")
            return []
        if isinstance(single, dict):
            return [single]
        if isinstance(single, list):
            return [item for item in single if isinstance(item, dict)]
        return []

    if not isinstance(outer_list, list):
        _log("[parse] 外层 JSON 不是 list，返回空列表")
        return []

    answers: list[dict] = []
    for item in outer_list:
        if isinstance(item, dict):
            answers.append(item)
            continue
        if not isinstance(item, str):
            continue
        try:
            item_json = json.loads(item)
        except (json.JSONDecodeError, ValueError) as e:
            _log(f"[parse] 单条 answer JSON 解析失败，跳过: {e}")
            continue
        if isinstance(item_json, dict):
            answers.append(item_json)
        elif isinstance(item_json, list):
            answers.extend(sub for sub in item_json if isinstance(sub, dict))
    return answers


# --------------------------------------------------------------------------- #
# 主入口：获取相似答案并解析 RD manager
# --------------------------------------------------------------------------- #
def fetch_similar_answers_with_rd_manager(
    content: str,
    access_token: str | None = None,
    uid: str = "qwertyuio-123456",
    name: str = "zhansan",
    url: str = "http://10.18.11.98:3000/api/v1/chat/completions",
    history_order: str = "earliest",
) -> str:
    """获取相似答案，并为每条相似 jira 解析 RD manager，返回 JSON 字符串。

    参数：
        history_order: 优先级 2 中多个历史 manager 的取舍方式，
                       "earliest"（默认，最早）或 "latest"（最晚）。
    """
    # 1. 获取 fastgpt 相似答案
    response = fastgpt_chat_completion(
        content=content,
        access_token=access_token,
        uid=uid,
        name=name,
        url=url,
    )
    answers = parse_similar_answers(response)
    if not answers:
        _log("[INFO] 没有解析到相似 jira")
        return json.dumps([], ensure_ascii=False)

    # 2. 复用同一个 jira 客户端（创建失败返回 None，不抛异常）
    jira_client = create_jira_client()
    if jira_client is None:
        _log("[jira] jira 客户端不可用，所有相似 jira 采用错误保底 manager")
        for answer in answers:
            answer["rd_manager"] = ERROR_MANAGER
            answer["rd_manager_priority"] = "优先级6：默认Manager（jira客户端不可用）"
        return json.dumps(answers, ensure_ascii=False, indent=2)

    # 3. 逐个解析 RD manager（单条处理异常时保底为默认 manager，不影响整体）
    for answer in answers:
        jira_id = str(answer.get("jira_id") or answer.get("key") or "").strip()
        manager, priority = get_manager_by_jira_id(
            jira_id, history_order=history_order, jira_client=jira_client
        )
        answer["rd_manager"] = manager
        answer["rd_manager_priority"] = priority
        _log(f"[RD Manager] {jira_id or '<未知jira>'} -> {manager}（{priority}）")

    return json.dumps(answers, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # 演示：需要先设置 JIRA_USERNAME / JIRA_PASSWORD 环境变量
    result = fetch_similar_answers_with_rd_manager("视频卡顿")
    print(result)
