"""
核心构建模块：为一个 jira_key 生成 (json_q, filter_a_json)。

流程（每个 Step 均带重试与具体日志）：
    Step1  获取并清洗 JIRA 基础数据（评论/描述/标题）
    Step2  并行 LLM：标题总结 + 评论总结/三要点提炼
    Step3  正则提取 问题总结/问题现象/复现步骤/comments 三要点
    Step4  填充 filter_a_json 基础 6 字段
    Step5  填充新增字段（chip/components/key_error_logs/root_cause/how_to_fix/
           patch_list/doc_link/source）
    Step6  LLM 提取 problem_causes（JSON 解析失败会重新调用 LLM，带重试）
    Step7  解析 rd_manager
    Step8  组装 json_q / json_a

任一步骤重试后仍失败 -> 抛 RetryFailedError，由 main 决定该 JIRA 不入库。
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 便于以脚本方式运行时也能 import 父级 find_similar_jira 的模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from utils.logger import log as mylog
from utils.llm_client import build_llm_client, LLMClient
from src.processor import (
    clean_comments,
    clean_description,
    summarize_comments_to_max_token,
)
from retry import run_with_retry, RetryFailedError
from jira_fields import JiraDataProvider


def _step1_fetch_and_clean(provider: JiraDataProvider, jira_id: str) -> tuple:
    """Step1：获取并清洗 JIRA 基础数据。"""
    comments_raw_all = provider.getComments(jira_id) or []
    cleaned_list, cleaned_str = clean_comments(comments_raw_all)
    description_raw = provider.getDescription(jira_id) or ""
    _, cleaned_description = clean_description(description_raw)
    summary = provider.getSummary(jira_id) or ""
    mylog(
        f"[Step1/8] {jira_id} 获取并清洗完成：评论原始{len(comments_raw_all)}条，"
        f"清洗后保留{len(cleaned_list)}条；描述清洗后{len(cleaned_description)}字符；标题为{summary[:40] or '<空>'}"
    )
    return cleaned_list, cleaned_str, cleaned_description, summary


def _run_summary_llm(llm_client: LLMClient, prompts: dict, summary: str, cleaned_description: str) -> str:
    """调用 LLM 生成 问题总结/问题现象/具体复现步骤。"""
    user_prompt = f"""以下是问题标题：
{summary}
以下是问题描述：
{cleaned_description}
"""
    return llm_client.qa_with_system(
        system_prompt=prompts.get("summary_system"),
        user_prompt=user_prompt,
    )


def _run_comments_llm(
    llm_client: LLMClient,
    prompts: dict,
    comments_list: list,
    max_token: int,
) -> tuple:
    """对评论进行分块总结，再提炼 复现方式与现象/定位结果/解决方案 三要点。"""
    if not comments_list:
        mylog("[Step2/8] 无有效评论，跳过评论总结与三要点提炼")
        return "", ""
    sc = summarize_comments_to_max_token(
        comments_list,
        max_token=max_token,
        llm_client=llm_client,
        system_prompt=prompts.get("comments_summary_system"),
        user_prompt_prefix=prompts.get("comments_summary_user_prefix"),
    )
    mylog(f"[Step2/8] 评论分块总结完成，共{len(sc)}字符")
    ec = llm_client.qa_with_system(
        system_prompt=prompts.get("comments_extract_system"),
        user_prompt="帮我总结以下内容：\n" + sc,
    )
    return sc, ec


def _step2_parallel_llm(
    llm_client: LLMClient,
    prompts: dict,
    cleaned_list: list,
    cleaned_str: str,
    cleaned_description: str,
    summary: str,
    max_token: int,
    retries: int,
    interval: float,
) -> tuple:
    """Step2：并行执行标题总结与评论总结/提炼。"""
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_summary = ex.submit(
            run_with_retry,
            "Step2-a 标题总结 LLM 调用",
            _run_summary_llm,
            retries,
            interval,
            (Exception,),
            llm_client,
            prompts,
            summary,
            cleaned_description,
        )
        fut_comments = ex.submit(
            run_with_retry,
            "Step2-b 评论总结与要点提炼 LLM 调用",
            _run_comments_llm,
            retries,
            interval,
            (Exception,),
            llm_client,
            prompts,
            cleaned_list,
            max_token,
        )
        resp = fut_summary.result()
        mylog(f"[Step2/8] 标题总结 LLM 返回{len(resp)}字符")
        summarize_comments, extract_summarize_comments = fut_comments.result()
        mylog(f"[Step2/8] 评论总结返回{len(summarize_comments)}字符，三要点提炼返回{len(extract_summarize_comments)}字符")
        return resp, summarize_comments, extract_summarize_comments


def _step3_extract(resp: str, extract_comments: str) -> tuple:
    """Step3：用正则从 LLM 结果中提取 问题总结/问题现象/复现步骤/三要点。"""
    summary_match = re.search(r"问题总结：([\s\S]*?)\s{2,}", resp)
    problem_summary = summary_match.group(1).strip() if summary_match else ""
    phenomenon_match = re.search(r"问题现象：([\s\S]*?)\s{2,}", resp)
    problem_phenomenon = phenomenon_match.group(1).strip() if phenomenon_match else ""
    steps_match = re.search(r"具体复现步骤：([\s\S]*)", resp)
    reproduce_steps = steps_match.group(1).strip() if steps_match else ""
    mylog(
        f"[Step3/8] 正则提取完成：问题总结{len(problem_summary)}字符、"
        f"问题现象{len(problem_phenomenon)}字符、复现步骤{len(reproduce_steps)}字符"
    )

    fx = re.search(r"\s*1、复现方式与现象:\s*(.*?)\s*2、定位结果:", extract_comments, re.S)
    dw = re.search(r"\s*2、定位结果:\s*(.*?)\s*3、解决方案:", extract_comments, re.S)
    jj = re.search(r"\s*3、解决方案:\s*(.*)", extract_comments, re.S)
    comments_results = {
        "复现方式与现象": fx.group(1).strip() if fx else "",
        "定位结果": dw.group(1).strip() if dw else "",
        "解决方案": jj.group(1).strip() if jj else "",
    }
    return problem_summary, problem_phenomenon, reproduce_steps, comments_results


def _step4_build_base(provider: JiraDataProvider, jira_id: str, problem_phenomenon: str, reproduce_steps: str, comments_results: dict) -> dict:
    """Step4：填充 filter_a_json 基础 6 字段。"""
    sw_version = provider.getSoftwareRelease(jira_id)
    hw_version = provider.getProjectId(jira_id)
    mylog(f"[Step4/8] {jira_id} 软件版本={sw_version or '<空>'}，硬件版本={hw_version or '<空>'}")
    comments = [
        {"level": level, "description": description}
        for level, description in comments_results.items()
        if description
    ]
    if len(comments) != len(comments_results):
        mylog(f"[Step4/8] {jira_id} comments 存在要点为空，仅保留非空条目，共{len(comments)}条")
    return {
        "jira_id": jira_id,
        "issue_description": problem_phenomenon,
        "reproduction_steps": reproduce_steps,
        "software_version": sw_version,
        "hardware_version": hw_version,
        "comments": comments,
    }


def _step5_extend_fields(provider: JiraDataProvider, jira_id: str, cleaned_description: str, retries: int, interval: float) -> dict:
    """Step5：逐个获取并填充新增字段（每个 getter 独立带重试）。"""
    extended = {}
    field_getters = {
        "chip": lambda: provider.getChip(jira_id),
        "components": lambda: provider.getComponents(jira_id),
        "key_error_logs": lambda: provider.getKeyErrorLogs(jira_id, cleaned_description),
        "root_cause": lambda: provider.getRootCause(jira_id) or "",
        "how_to_fix": lambda: provider.getHowToFix(jira_id) or "",
        "patch_list": lambda: provider.getPatchList(jira_id),
        "doc_link": lambda: provider.getDocLink(jira_id),
        "source": lambda: provider.getSource(jira_id),
    }
    for field_name, getter in field_getters.items():
        value = run_with_retry(
            f"Step5 读取字段 {field_name}@{jira_id}",
            getter,
            retries,
            interval,
            (Exception,),
        )
        extended[field_name] = value
        mylog(f"[Step5/8] {jira_id} 字段 {field_name}={value if isinstance(value, str) else value}")
    return extended


def _step6_extract_problem_causes(llm_client: LLMClient, prompts: dict, raw_brief: str) -> list:
    """Step6：调用 LLM 提取 problem_causes；JSON 解析失败抛 ValueError 触发外层重试（重新调 LLM）。"""
    user_prompt = f"""
    请分析以下内容，提取可能的问题原因，以列表形式返回：
    {raw_brief}
    """
    raw = llm_client.qa_with_system(
        system_prompt=prompts.get("root_cause_system"),
        user_prompt=user_prompt,
    )
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"LLM 返回内容不是合法 JSON：{exc}；内容前120字：{str(raw)[:120]}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM 返回 JSON 不是对象：{str(parsed)[:120]}")
    causes = parsed.get("problem_causes")
    if not isinstance(causes, list):
        raise ValueError("LLM 返回 JSON 缺少 problem_causes 列表")
    result = [str(cause).strip() for cause in causes if str(cause).strip()]
    mylog(f"[Step6/8] 提取 problem_causes 共{len(result)}条")
    return result


def build_q_a(
    jira_id: str,
    prompts: dict,
    provider: JiraDataProvider,
    llm_client: LLMClient | None = None,
) -> tuple:
    """为单个 jira_key 构建 (json_q, filter_a_json)。

    参数：
        jira_id:    JIRA key，例如 "OTT-80575"。
        prompts:    提示词 dict（来自 config.load_prompts()）。
        provider:   JiraDataProvider 实例（jira 数据读取）。
        llm_client: LLM 客户端；不传则按 config.LLM_PRESET 自动创建。
    返回：
        (json_q, filter_a_json)。
    抛出：
        任一环节重试后仍失败抛 RetryFailedError。
    """
    retries = cfg.RETRY_TIMES
    interval = cfg.RETRY_INTERVAL
    max_token = int(cfg.MAX_TOKEN)
    if llm_client is None:
        llm_client = build_llm_client(preset_name=cfg.LLM_PRESET)

    # ---- Step1：获取并清洗 ----
    cleaned_list, cleaned_str, cleaned_description, summary = run_with_retry(
        f"Step1 获取并清洗 {jira_id} 的基础数据",
        _step1_fetch_and_clean,
        retries,
        interval,
        (Exception,),
        provider,
        jira_id,
    )

    # ---- Step2：并行 LLM ----
    resp, summarize_comments, extract_comments = _step2_parallel_llm(
        llm_client, prompts, cleaned_list, cleaned_str, cleaned_description,
        summary, max_token, retries, interval,
    )

    # ---- Step3：正则提取 ----
    problem_summary, problem_phenomenon, reproduce_steps, comments_results = _step3_extract(resp, extract_comments)

    # ---- Step4：基础 6 字段 ----
    filter_a_json = run_with_retry(
        f"Step4 填充 {jira_id} 基础字段",
        _step4_build_base,
        retries,
        interval,
        (Exception,),
        provider,
        jira_id,
        problem_phenomenon,
        reproduce_steps,
        comments_results,
    )

    # ---- Step5：新增字段 ----
    # 打包成一个整体步骤重试：任一新字段 getter 多次失败则该条不入库
    extended = run_with_retry(
        f"Step5 填充 {jira_id} 新增字段",
        _step5_extend_fields,
        retries,
        interval,
        (Exception,),
        provider,
        jira_id,
        cleaned_description,
        retries,
        interval,
    )
    filter_a_json.update(extended)

    # ---- Step6：提取 problem_causes ----
    raw_brief = json.dumps(filter_a_json, ensure_ascii=False, indent=2)
    problem_causes = run_with_retry(
        f"Step6 提取 {jira_id} 的 problem_causes",
        _step6_extract_problem_causes,
        retries,
        interval,
        (Exception,),
        llm_client,
        prompts,
        raw_brief,
    )
    filter_a_json["problem_causes"] = problem_causes

    # ---- Step7：解析 rd_manager ----
    rd_manager = run_with_retry(
        f"Step7 解析 {jira_id} 的 rd_manager",
        provider.getRdManager,
        retries,
        interval,
        (Exception,),
        jira_id,
    )
    filter_a_json["rd_manager"] = rd_manager

    # ---- Step8：组装 json_q / json_a ----
    json_q = (problem_summary or problem_phenomenon or "").strip()
    json_a = json.dumps(filter_a_json, ensure_ascii=False)
    mylog(f"[Step8/8] {jira_id} 组装完成：json_q={len(json_q)}字符，json_a={len(json_a)}字符")
    if not json_q:
        mylog(f"[Step8/8] {jira_id} 警告：problem_summary 与 problem_phenomenon 均为空，json_q 为空字符串")
    return json_q, filter_a_json