from __future__ import annotations
import os
import re
import requests
from difflib import SequenceMatcher
from utils.llm_client import LLMClient, LLMConfig, build_llm_client


SECTION_ALIASES = {
    "description": ["description", "描述", "问题描述", "详情"],
    "comments": ["comments", "评论", "comment"],
    "summary": ["summary", "标题", "概要"],
}


def split_sections(text: str) -> dict[str, str]:
    lines = text.splitlines()
    current_key = "body"
    sections: dict[str, list[str]] = {"body": []}
    for line in lines:
        normalized = line.strip().lower().lstrip("#").strip()
        matched_key = None
        for key, aliases in SECTION_ALIASES.items():
            if normalized in [alias.lower() for alias in aliases]:
                matched_key = key
                break
        if matched_key:
            current_key = matched_key
            sections.setdefault(current_key, [])
            continue
        sections.setdefault(current_key, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _dedupe_lines(lines: list[str], similarity: float) -> list[str]:
    kept: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if any(SequenceMatcher(None, line, other).ratio() >= similarity for other in kept):
            continue
        kept.append(line)
    return kept


import json

MAX_COMMENT_LEN = 2000
BASE64_THRESHOLD = 200

KEYWORDS = (
    'error', 'Error', 'failed', 'Fail',
    'exception', 'panic',
    "Can't", 'cannot',
    'invalid', 'format'
)


def has_long_base64(text: str) -> bool:
    pattern = rf'[A-Za-z0-9+/=]{{{BASE64_THRESHOLD},}}'
    return bool(re.search(pattern, text))


def json_ratio(text: str) -> float:
    """
    Roughly detect JSON / structured noise ratio
    """
    if not text:
        return 0.0
    json_chars = sum(text.count(c) for c in '{}[]":,')
    return json_chars / len(text)


def extract_key_info(text: str) -> str:
    lines = (l.strip() for l in text.splitlines())
    hits = [l for l in lines if any(k in l for k in KEYWORDS)]
    return '\n'.join(hits)


def should_drop_comment(raw: str) -> bool:
    """
    Strong drop rules
    """
    # 极端长度
    if len(raw) > 8000:
        return True

    # csr + base64 = 直接丢弃
    if '"csr"' in raw and has_long_base64(raw):
        return True

    # JSON 噪音占比过高
    if json_ratio(raw) > 0.6:
        return True

    return False


def filter_comments(comments: list[str]) -> list[str]:
    """
    Input : raw comments list
    Output: cleaned & meaningful comments
    """
    results = []
    # 删除以下类型内容：
    # !image-xxx.png!
    # !screenshot-xxx.png!
    # !screenshot-xxx.png|thumbnail!
    # [~username]  (username 任意)
    pattern = re.compile(
        r'!(?:image|screenshot)-[\w\-]+\.png(?:\|thumbnail)?!|\[~[^\]]+\]'
    )

    exclude_keywords = ["详细信息请参见链接","Change merged", "Change proposed", "AI智能分析"]

    for raw in comments:
        if not raw or not raw.strip():
            continue
        if any(keyword in raw for keyword in exclude_keywords):
            continue
        text = raw

        # 删除匹配内容，并合并多余空格
        cleaned = re.sub(pattern, '', text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        text = cleaned
        # 第一层强过滤
        if should_drop_comment(text):
            # 第二层：提取有意义内容
            summary = extract_key_info(text)
            if not summary:
                continue
            else:
                text = summary
        # 最终截断
        if len(text) > MAX_COMMENT_LEN:
            text = text[:MAX_COMMENT_LEN] + '\n[TRUNCATED]'

        results.append(text)

    return results

def _clean_text(text: any, max_line_length: int, similarity: float) -> str:
    if type(text) == str:
        raw_lines = [line.strip() for line in text.splitlines()]
    else:
        raw_lines = text
    filtered = filter_comments(raw_lines)
    deduped = _dedupe_lines(filtered, similarity)
    return deduped, "\n".join(deduped).strip()


def clean_comments(text: str) -> str:
    return _clean_text(text, max_line_length=600, similarity=0.9)


def clean_description(text: str) -> str:
    return _clean_text(text, max_line_length=800, similarity=0.9)


def tokenize_text(
    text: str,
    include_special_tokens: bool = False,
    url: str = "http://10.58.11.60:1234/tokenize",
) -> int:
    payload = {"text": text or "", "include_special_tokens": include_special_tokens}
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    token = response.json().get('token_count')
    return token


def compare_similarity_http(
    user_causes: list[str],
    similar_causes: list[str],
    url: str = "http://10.18.11.98:1235/compare/sklearn",
) -> dict:
    payload = {"user_causes": user_causes, "similar_causes": similar_causes}
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def summarize_comments_to_max_token(
    comments_list: list[str],
    max_token: int,
    llm_client: LLMClient,
    system_prompt: str,
    user_prompt_prefix: str = "请总结以下内容：",
) -> str:
    """
    分块总结
    """
    if not comments_list:
        return ""

    def build_batches(items: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        for item in items:
            if not item:
                continue
            candidate = current + [item]
            token_count = tokenize_text("\n".join(candidate))
            if token_count <= max_token or not current:
                current = candidate
                continue
            batches.append(current)
            current = [item]
        if current:
            batches.append(current)
        return batches

    current_items = comments_list
    previous_tokens: int | None = None
    while True:
        batches = build_batches(current_items)
        if len(batches) == 1:
            return "\n".join(batches[0])
        summaries: list[str] = []
        for batch in batches:
            text = "\n".join(batch)
            user_prompt = f"{user_prompt_prefix}\n{text}"
            summary = llm_client.qa_with_system(system_prompt=system_prompt, user_prompt=user_prompt)
            if summary:
                summaries.append(summary)
        combined = "\n".join(summaries).strip()
        if not combined:
            return ""
        token_count = tokenize_text(combined)
        if token_count <= max_token:
            return combined
        if previous_tokens is not None and token_count >= previous_tokens and len(summaries) == 1:
            return combined
        previous_tokens = token_count
        current_items = summaries


def extract_key_fields(description: str) -> dict[str, str]:
    fields = {
        "repro_steps": _extract_block(description, ["复现步骤", "repro steps", "steps to reproduce"]),
        "phenomenon": _extract_block(description, ["问题现象", "现象", "symptom"]),
        "error_logs": _extract_block(description, ["错误日志", "log", "error log", "stack trace"]),
    }
    return fields


def _extract_block(text: str, keywords: list[str]) -> str:
    if not text:
        return ""
    pattern = "|".join(re.escape(word) for word in keywords)
    match = re.search(rf"({pattern})[:：]?(.*)", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    block = match.group(2).strip()
    return block


# llm_client = build_llm_client(preset_name="ollama_qwen3_8b-q8")
# # prompt_text = llm_client._build_prompt("你是谁？", "")
# user_prompt = """
# 帮我总结以下内容：
# """
# system_prompt = """
# ## 角色
# 你是一个技术总结助手。你的任务是根据用户提供的评论，将其整理为**完整的陈述句总结**。总结要求如下：

# ## 任务
# 1. **保留所有关键信息**：
#    - 设备名称和类型
#    - 测试日期
#    - 设备现象（如黑屏、加载异常）
#    - 关键日志信息（WARN、错误码、接口URL等）
#    - 播放状态或异常
#    - 已知原因或错误（如OOM、IP-9）

# 2. **禁止推测或补充任何未明确出现的信息**。

# 3. **输出格式**：
#    - 单段完整陈述句，信息按设备顺序呈现。
#    - 保持日志和属性的原始表达，不修改数值或字段。
#    - 必须用中文描述。

# ## 示例格式说明（仅作演示，不可直接使用）：
# 示例输入：
# # 1台无线非裁剪音轨切换烤机（钟卫工位）-27日过来的现象：黑屏一直在加载 ## sendWatchLiveChannel: WARN: http error code = 404. [PERF] 498ms, url='[https://api.claro.com.br/residential/v1/userusages/contents'] ## 播放501 dash， 黑屏怀疑是app拉不到数据，因为app 访问license 也返回了404 # 1台无线裁剪音轨切换烤机（机顶盒2）-27日过来的现象：黑屏一直在加载 ## 【原因】有oom导致的IP-9

# 期望输出：
# 无线非裁剪音轨切换烤机（钟卫工位）在27日测试过程中出现黑屏一直在加载，日志显示“sendWatchLiveChannel: WARN: http error code = 404. [PERF] 498ms, url='https://api.claro.com.br/residential/v1/userusages/contents'”，播放501 dash时黑屏，且App访问license接口也返回404；无线裁剪音轨切换烤机（机顶盒2）在27日测试过程中同样出现黑屏一直在加载，日志显示有OOM导致IP-9。
# """
# resp = llm_client.qa_with_system(system_prompt=system_prompt, user_prompt=user_prompt)
# print(resp)
# ret = tokenize_text(text="hello world!")
# print(ret)


import requests

access_token = "fastgpt-hDRWg3sKXu2mdZaTvXmfv1kaL6uAMxkrQO9oTRpm60WefI200D4Ra27C"
def fastgpt_chat_completion(
    content: str,
    access_token: str | None = None,
    uid: str = "qwertyuio-123456",
    name: str = "zhansan",
    url: str = "http://10.18.11.98:3000/api/v1/chat/completions",
) -> dict:
    print(f"content:{content}")
    token = access_token or os.getenv("FASTGPT_API_KEY", "fastgpt-hDRWg3sKXu2mdZaTvXmfv1kaL6uAMxkrQO9oTRpm60WefI200D4Ra27C")
    if not token:
        raise RuntimeError("未提供 FASTGPT_API_KEY")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "stream": False,
        "detail": False,
        "variables": {"uid": uid, "name": name},
        "messages": [{"role": "user", "content": content}],
    }
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    res = response.json()["choices"][0]["message"]["content"]
    return res



def similar_answers_to_dict(raw_text):
    result = {}
    pattern = r'相似的answer有：([\s\S]*)'
    answer_match = re.search(pattern, raw_text, re.S)

    FIELDS_TO_REMOVE = {"software_version", "hardware_version", "chip"}

    if answer_match:
        ans_list_str = answer_match.group(1).strip()

        try:
            # 外层是 list[str]
            outer_list = json.loads(ans_list_str)

            serialized_answers = []

            for item_str in outer_list:
                try:
                    item_json = json.loads(item_str)

                    # 🚫 强制删除指定字段
                    # for field in FIELDS_TO_REMOVE:
                    #     item_json.pop(field, None)

                    # ✅ 序列化为字符串
                    serialized_answers.append(
                        json.dumps(item_json, ensure_ascii=False)
                    )

                except json.JSONDecodeError:
                    serialized_answers.append(item_str)

            # similar_answers 本身就是序列化结果
            result["similar_answers"] = serialized_answers

            # similar_answers_str 与其完全同步
            result["similar_answers_str"] = "\n".join(serialized_answers)

        except json.JSONDecodeError:
            result["similar_answers"] = [ans_list_str]
            result["similar_answers_str"] = ans_list_str
    else:
        result["similar_answers"] = []
        result["similar_answers_str"] = ""
    return result["similar_answers"]


def fetch_similar_answers(
    content: str,
    access_token: str | None = None,
    uid: str = "qwertyuio-123456",
    name: str = "zhansan",
    url: str = "http://10.18.11.98:3000/api/v1/chat/completions",
):
    response = fastgpt_chat_completion(
        content=content,
        access_token=access_token,
        uid=uid,
        name=name,
        url=url,
    )
    return similar_answers_to_dict(response)

if __name__ == "__main__":
   ret = fetch_similar_answers("视频卡顿", access_token=access_token)
   print(f"ret={ret}")
