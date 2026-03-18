from __future__ import annotations

from dataclasses import dataclass
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.analyzer import (
    compare_similarity,
)
from src.processor import (
    clean_comments, 
    clean_description, 
    extract_key_fields, 
    split_sections, 
    summarize_comments_to_max_token, 
    fetch_similar_answers,
)
from utils.llm_client import build_llm_client
from utils.jira_client import MyJira
from utils.logger import log as mylog
import re
import json

@dataclass
class SimilarItem:
    jira_id: str
    scenario: str
    root_cause: str
    score: float
    reason: str


def write_report(
    output_dir: str,
    query_jira_id: str,
    query_summary: str,
    query_root_cause: str,
    items: list[SimilarItem],
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(output_dir) / f"jira_{query_jira_id}_report_{timestamp}.md"
    lines = [
        f"# Jira 相似性分析报告 - {query_jira_id}",
        "",
        f"查询 Jira：{query_summary}",
        "",
        "## Root Cause",
        "",
        query_root_cause or "暂无",
        "",
        "## 相似 Jira 列表",
        "",
        "| 条目 | 触发场景/测试用例 | 根因信息 | 相似度(0–10) | 相似原因简述 |",
        "|------|------------------|-----------|---------------|---------------|",
    ]
    for item in items:
        lines.append(
            f"| {item.jira_id} | {item.scenario} | {item.root_cause} | {item.score:.2f} | {item.reason} |"
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return str(output_path)


# def collect_recent_reports(config: dict) -> list[JiraReport]:
#     reports_glob = config.get("reports_glob", "reports/*/*_last_retrieve_report.md")
#     days_window = int(config.get("days_window", 2))
#     paths = find_recent_reports(reports_glob, days_window)
#     return load_reports(paths)
def similarity_desc(score: float) -> str:
    if score >= 7:
        return "根因高度相似，可能为同一类启动链路问题"
    if score >= 4:
        return "存在一定关联，可能发生在相同启动阶段"
    return "根因不同，场景或模块差异明显"


def _normalize_problem_causes(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("problem_causes", [])
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(value)]


def _normalize_similar_answer(value) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return _normalize_similar_answer(parsed)
    return []


def _log_elapsed(step: str, start: float) -> None:
    mylog(f"{step} 耗时 {time.perf_counter() - start:.3f}s")


def build_similarity_md(unique_similar_answers, filter_a_json):
    md = []
    md.append("| 条目 | 触发场景/测试用例 | 根因信息 | 相似度(0–10) | 相似原因简述 |")
    md.append("|------|------------------|-----------|---------------|---------------|")
    user_causes = _normalize_problem_causes(filter_a_json.get("problem_causes"))
    user_causes_text = "；".join(user_causes)
    user_jira_id = filter_a_json.get('jira_id', '')
    md.append(
        f"| {user_jira_id} | {filter_a_json.get('issue_description', '')} | "
        f"{user_causes_text} | - | - |"
    )
    rows = []
    for item in unique_similar_answers:
        item_data = item if isinstance(item, dict) else {}
        problem_causes = _normalize_problem_causes(item_data.get("problem_causes"))
        _, max_score = compare_similarity(user_causes, problem_causes)
        score = max_score * 10 if max_score <= 1 else max_score
        desc = similarity_desc(score)
        similar_jira_id = item_data.get('jira_id', '')
        if user_jira_id != similar_jira_id:
            row = (
                f"| {similar_jira_id} | {item_data.get('issue_description', '')} | "
                f"{'；'.join(problem_causes)} | {score:.2f} | {desc} |"
            )
            rows.append((score, row))
    rows.sort(key=lambda x: x[0], reverse=True)
    for _, row in rows:
        md.append(row)
    return md


def run_pipeline( config: dict | None = None, key: str | None = None):
    config = config or {}
    prompts = config.get("prompts", {})
    if not key:
        raise ValueError("key is required")
    total_start = time.perf_counter()
    reports_dir = Path(f"reports/{key}")
    for offset in range(2):
        date_str = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        existing_path = reports_dir / f"{key}_{date_str}_report.md"
        if existing_path.exists():
            mylog(f"命中两天内报告:{existing_path}")
            _log_elapsed("总耗时", total_start)
            return existing_path.read_text(encoding="utf-8")

    # llm_client = build_llm_client(config.get("llm"))
    # weight_root = float(config.get("similarity", {}).get("weight_root", 0.8))
    # weight_log = float(config.get("similarity", {}).get("weight_log", 0.2))
    # output_dir = config.get("output_dir", "reports")

    step_start = time.perf_counter()
    llm_client = build_llm_client(preset_name=config.get("llm_preset", "ollama_qwen3_8b-q8"))
    my_jira = MyJira("https://jira.amlogic.com", "lingzhi.bi", "Qwer!23456")
    _log_elapsed("初始化客户端", step_start)

    step_start = time.perf_counter()
    comments = my_jira.getComments(key)
    # mylog(f"comments: {comments}")
    cleaned_comments_list, cleaned_comments_str = clean_comments(comments)
    mylog(f"cleaned_comments: {cleaned_comments_str}")
    description = my_jira.getDescription(key)
    _, cleaned_description = clean_description(description)
    # mylog(f"cleaned_description: {cleaned_description}")
    summary = my_jira.getSummary(key)
    # mylog(f"summary: {summary}")
    root_cause = my_jira.getRootCause(key)
    # mylog(f"root_cause: {root_cause}")
    how_to_fix = my_jira.getHowToFix(key)
    # mylog(f"how_to_fix: {how_to_fix}")
    _log_elapsed("获取并清洗 Jira 数据", step_start)

    conbine_summary = f"""
    # {summary}

    ## description:
    {cleaned_description}

    ## root_cause:
    {root_cause}

    ## how_to_fix:
    {how_to_fix}

    ## comments:
    {cleaned_comments_str}
    """

    
    user_prompt = f"""以下是问题标题：
    {summary}
    以下是问题描述：
    {cleaned_description}
    """
    system_prompt = prompts.get(
        "summary_system",
        """
    ## 角色：
    你是一名专业的问题总结助手。

    ## 任务：
    根据提供的信息，严格生成以下内容，禁止输出其他无关信息：

    1. **问题总结**：  
    - 用一句话概括问题发生的操作和结果 
    - **仅当涉及软件版本时需进行模糊化**（如具体 ROM 版本号、系统版本号） 
    - 软件版本统一替换为：**“某版本”** 
    - 设备型号、测试名称、异常类型等其他专有名词可正常保留

    2. **问题现象**：  
    - 简洁描述实际观察到的异常表现 
    - 可保留具体技术名词、测试名称、异常类型及设备信息 
    - **不对软件版本以外的信息做模糊处理**

    3. **具体复现步骤**：  
    - 使用 Step1、Step2、Step3 格式详细列出复现流程 
    - 包含关键操作、环境、版本或依赖条件 
    - **步骤中若出现软件版本，仅替换为“某版本”** 


    ## 输出要求：
    - 每个字段独立清晰，不允许合并或遗漏  
    - 严禁添加“注：”“备注：”或其他多余文字  
    - 使用 Markdown 或文本格式均可  
    - 保持简洁、专业、易读
    - **禁止出现具体的专有名称**（例如频道编号、确切文件名、设备型号、应用名称等），用“某频道”“某设备”“某片源”等替代。

    ## 示例格式说明（仅作演示，不可直接使用）：
    
    问题总结：在某版本系统环境下执行 test_monkey 自动化测试时，触发 kernel_panic 导致测试中断 
    问题现象：执行 test_monkey 过程中发生 kernel_panic，系统直接崩溃 
    具体复现步骤： 
    Step1：在某版本 Rom 环境下启动 test_monkey 测试 
    Step2：执行自动化测试流程并模拟用户操作 
    Step3：测试过程中系统触发 kernel_panic，测试流程终止
    """,
    )
    # mylog(f"conbine_summary: {conbine_summary}")
    step_start = time.perf_counter()
    resp = llm_client.qa_with_system(system_prompt=system_prompt, user_prompt=user_prompt)
    mylog(f"resp:{resp}")
    _log_elapsed("LLM 问题总结", step_start)
    
    # 提取问题总结
    summary_match = re.search(r"问题总结：([\s\S]*?)\s{2,}", resp)
    problem_summary = summary_match.group(1).strip() if summary_match else ""

    # 提取问题现象
    phenomenon_match = re.search(r"问题现象：([\s\S]*?)\s{2,}", resp)
    problem_phenomenon = phenomenon_match.group(1).strip() if phenomenon_match else ""

    # 提取具体复现步骤
    steps_match = re.search(r"具体复现步骤：([\s\S]*)", resp)
    reproduce_steps = steps_match.group(1).strip() if steps_match else ""

    summarize_user_prompt = prompts.get(
        "comments_summary_user_prefix",
        "帮我总结以下内容：",
    )
    summarize_sys_prompt = prompts.get(
        "comments_summary_system",
        """
    ## 角色
    你是一个技术总结助手。你的任务是根据用户提供的评论，将其整理为**完整的陈述句总结**。总结要求如下：

    ## 任务
    1. **保留所有关键信息**：
    - 设备名称和类型
    - 测试日期
    - 设备现象（如黑屏、加载异常）
    - 关键日志信息（WARN、错误码、接口URL等）
    - 播放状态或异常
    - 已知原因或错误（如OOM、IP-9）

    2. **禁止推测或补充任何未明确出现的信息**。

    3. **输出格式**：
    - 单段完整陈述句，信息按设备顺序呈现。
    - 保持日志和属性的原始表达，不修改数值或字段。
    - 必须用中文描述。

    ## 示例格式说明（仅作演示，不可直接使用）：
    示例输入：
    # 1台无线非裁剪音轨切换烤机（钟卫工位）-27日过来的现象：黑屏一直在加载 ## sendWatchLiveChannel: WARN: http error code = 404. [PERF] 498ms, url='[https://api.claro.com.br/residential/v1/userusages/contents'] ## 播放501 dash， 黑屏怀疑是app拉不到数据，因为app 访问license 也返回了404 # 1台无线裁剪音轨切换烤机（机顶盒2）-27日过来的现象：黑屏一直在加载 ## 【原因】有oom导致的IP-9

    期望输出：
    无线非裁剪音轨切换烤机（钟卫工位）在27日测试过程中出现黑屏一直在加载，日志显示“sendWatchLiveChannel: WARN: http error code = 404. [PERF] 498ms, url='https://api.claro.com.br/residential/v1/userusages/contents'”，播放501 dash时黑屏，且App访问license接口也返回404；无线裁剪音轨切换烤机（机顶盒2）在27日测试过程中同样出现黑屏一直在加载，日志显示有OOM导致IP-9。
    """,
    )

    max_token = int(config.get("max_token", 2096))
    step_start = time.perf_counter()
    summarize_comments = summarize_comments_to_max_token(
        cleaned_comments_list,
        max_token=max_token,
        llm_client=llm_client,
        system_prompt=summarize_sys_prompt,
        user_prompt_prefix=summarize_user_prompt,
    )
    mylog(f"summarize_comments:{summarize_comments}")
    _log_elapsed("LLM 评论摘要", step_start)

    filter_a_json = {
        "jira_id":"",
        "issue_description": "",
        "reproduction_steps": [],
        "software_version": "",
        "hardware_version": "",
        "comments": []
    }

    user_prompt = f"""
    帮我总结以下内容：
    {summarize_comments}
    """
    system_prompt = prompts.get(
        "comments_extract_system",
        """
    ## 角色
    你是一名 Jira 分析专家，负责根据用户提供的 comments 内容，自动提炼并总结问题的关键结论。

    ## 任务
    从用户输入的 Jira comments 中，抽取并分别生成三条 **一句话总结**，每条必须包含关键细节（如：播放顺序、log 表现、属性值、关键现象等）。  
    若 comments 中 **缺少某一项的信息（复现方式/定位结果/解决方案）**，则该项返回 **空字符串**。


    ## 输出要求
    - 每个要点必须为一句话，不可多句。
    - 必须包含关键细节（如 log 现象、属性名、播放顺序、错误状态特征等）。
    - 若某项在 comments 中找不到任何相关信息，则该项输出为空。
    - 不得虚构不存在的信息。
    - **禁止出现具体专有名称与禁止出现人名**，包括但不限于：具体频道号、工程师姓名、确切文件名、设备型号、应用名称、内部代码文件名等。
    - 如需指代，可使用“某频道”“某片源”“某配置”“某功能模块”“相关人员”等抽象替代描述。

    ## 请严格按以下输出格式输出：

    1、复现方式与现象:一句话总结（带关键细节），如缺失则返回空

    2、定位结果:一句话总结（带关键细节），如缺失则返回空

    3、解决方案:一句话总结（带关键细节），如缺失则返回空


    """,
    )

    step_start = time.perf_counter()
    extract_summarize_comments = llm_client.qa_with_system(system_prompt=system_prompt, user_prompt=user_prompt)
    _log_elapsed("LLM 评论结构化", step_start)

    fx = re.search(r"\s*1、复现方式与现象:\s*(.*?)\s*2、定位结果:", extract_summarize_comments, re.S)
    dw = re.search(r"\s*2、定位结果:\s*(.*?)\s*3、解决方案:", extract_summarize_comments, re.S)
    jj = re.search(r"\s*3、解决方案:\s*(.*)", extract_summarize_comments, re.S)
    results = {
        "复现方式与现象": fx.group(1).strip() if fx else "",
        "定位结果": dw.group(1).strip() if dw else "",
        "解决方案": jj.group(1).strip() if jj else ""
    }
    # return [{"json": result}]
    for level,item in results.items():
        filter_a_json["comments"].append({
                    "level": level,
                    "description": item
                })
    sw_version = my_jira.getSoftwareRelease(key)
    hw_version = my_jira.getProjectId(key)
    filter_q = problem_summary
    filter_a_json["jira_id"] = key
    filter_a_json["issue_description"] = problem_phenomenon
    filter_a_json["reproduction_steps"] = reproduce_steps
    filter_a_json["software_version"] = sw_version
    filter_a_json["hardware_version"] = hw_version
    # filter_a_json = {'jira_id': 'OTT-85206', 'issue_description': '双解码操作过程中，OTT-83766补丁标志被意外重置，导致PCM模式下出现8db差异', 'reproduction_steps': 'Step1：开启双解码功能  \nStep2：修改配置文件中FAST_CHANNEL_DURATION参数为120  \nStep3：在某版本系统环境下执行频道切换测试，涉及AAC与AC3格式的双向切换，观察补丁标志状态及PCM模式差异', 'software_version': 'Android U-14', 'hardware_version': 'BL20AR-S905X5', 'comments': [{'level': '复现方式与现象', 'description': '在双解码开启状态下，通过频道切换（ch18→ch19→ch20）导致某频道音量降低，部分测试场景偶现音量降低现象'}, {'level': '定位结果', 'description': '客户应用某补丁后，patch_src未正确管理导致异常，需检查输入端口修复'}, {'level': '解决方案', 'description': '需更新某库并应用指定补丁，测试环境需使用指定固件及补丁文件'}]}
    # filter_q = "在双解码启用状态下执行频道切换测试时，PCM模式下出现8db差异"
    mylog(f"filter_a_json:{filter_a_json}")
    user_prompt = f"""
    请分析以下内容，提取可能的问题原因，以列表形式返回：
    {filter_a_json}
    """
    system_prompt = prompts.get(
        "root_cause_system",
        """
    ### 角色设定
    你是一名资深 Android / 多媒体系统问题分析专家，擅长从 JIRA 的  
    `issue_description` 和 `comments` 中抽取**真正导致问题发生的根因（problem causes）**，用于问题相似度匹配与归类。

    ---

    ### 任务目标
    根据输入的 JIRA 内容，分析**是什么原因导致问题**，输出可直接用于相似度计算的问题点列表。

    ---

    ### 严格分析规则
    1. 每条只保留 **直接导致问题的根因**
    2. 回答“问题是因为什么原因而发生的”，
    3. 若存在多个独立问题原因，必须拆分为多条
    4. **禁止输出**：
    - 复现步骤或操作场景  
    - 函数调用链、库名、崩溃地址、信号类型  
    - 已知问题或修复方法  
    - 编号、解释文字或多余字段  
    5. 输出内容必须是**完整的一句话**，可独立用于相似度匹配  
    6. 一条根因一句话，不允许把多层因果合并
    7. 每一条必须是完整的陈述句
    - 必须包含：原因主体 + 触发/缺陷行为 + 导致结果
    - 必须能单独回答：“这个问题为什么会发生？”
    - 禁止输出名词、短语或关键词堆砌
    - 错误示例：“内存越界”“DDR位翻转”“buffer manager 初始化失败”
    - 正确示例：“由于 buffer manager 初始化失败，内核在后续访问中触发空指针解引用并导致 kernel panic”
    8. 简洁明了，一句话就能说明白
    9. 输出必须严格遵守 JSON 格式：

    {
    "problem_causes": [
        "问题原因 1",
        "问题原因 2"
    ]
    }
    """,
    )
    # 提取问题点
    similar_answers = []
    step_start = time.perf_counter()
    similar_answer = fetch_similar_answers(filter_q)
    similar_answers.extend(similar_answer)
    
    extract_user_problem_causes = llm_client.qa_with_system(system_prompt=system_prompt, user_prompt=user_prompt)
    mylog(f"extract_user_problem_causes:{extract_user_problem_causes}")
    extract_user_problem_causes = json.loads(extract_user_problem_causes)['problem_causes']
    filter_a_json['problem_causes'] = extract_user_problem_causes
    unique_similar_answers = []
    for extract_problem_cause in extract_user_problem_causes:
        similar_answer = fetch_similar_answers(extract_problem_cause)
        mylog(f"similar_answer:{similar_answer}")
        similar_answers.extend(similar_answer)
    _log_elapsed("LLM 根因与相似问题检索", step_start)

    # 去重
    step_start = time.perf_counter()
    seen = set()
    for similar_answer in similar_answers:
        mylog(f"similar_answer type:{type(similar_answer)}")
        for similar_answer_json in _normalize_similar_answer(similar_answer):
            similar_answer_jira_id = similar_answer_json.get('jira_id')
            similar_answer_sw_verison = similar_answer_json.get('software_version')
            if similar_answer_jira_id not in seen:
                #过滤软件版本
                if similar_answer_sw_verison and sw_version and sw_version == similar_answer_sw_verison:
                    seen.add(similar_answer_jira_id)
                    unique_similar_answers.append(similar_answer_json)
    if not unique_similar_answers:
        mylog("没有相似的jira")        
        _log_elapsed("相似问题去重", step_start)
        _log_elapsed("总耗时", total_start)
        return "没有相似的jira"
    _log_elapsed("相似问题去重", step_start)
    mylog(f"unique_similar_answers:{unique_similar_answers}")
    step_start = time.perf_counter()
    md = build_similarity_md(unique_similar_answers, filter_a_json)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{key}_{datetime.now().strftime('%Y%m%d')}_report.md"
    with open(report_path, "w", encoding="utf-8") as file_handle:
        output_md = "\n".join(md)
        file_handle.write(output_md)
    _log_elapsed("生成报告", step_start)
    _log_elapsed("总耗时", total_start)
    return output_md

        


    # filter_a = f"""
    # jira_id: {key}
    
    # 现象：
    # {problem_phenomenon}
    
    # 复现步骤：
    # {reproduce_steps}
    
    # 软件版本：
    # {sw_version}

    # 硬件版本：
    # {hw_version}

    # 评论：
    # {extract_summarize_comments}
    # """

    # mylog(f"filter_a:{filter_a}")
    


if __name__ == "__main__":
    run_pipeline()

# /home/bj17300-049u/work/find_similar_jira/311venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
