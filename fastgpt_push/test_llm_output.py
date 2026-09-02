"""
LLM 输出质量诊断测试：用于确认 10.58.11.60:11434 的 LLM 服务是否持续返回垃圾内容
（此前出现返回英文儿童故事、长时间 503 等现象）。

用法示例：
    python fastgpt_push/test_llm_output.py                # json + summary 各测 3 次
    python fastgpt_push/test_llm_output.py --count 5      # 两类各测 5 次
    python fastgpt_push/test_llm_output.py --type json    # 只测 JSON 类探测
    python fastgpt_push/test_llm_output.py --interval 10  # 每次探测之间间隔 10 秒

说明：
- 不做重试，保留每次调用的原始结果（异常/垃圾都要如实上报）；
- 判定规则：
    * 调用抛异常                    -> "调用失败"
    * 要求 JSON 但 json.loads 失败  -> "不可用(非JSON)"
    * 缺少目标关键词                -> "不可用(缺关键词)"
    * 内容超长且中文字符占比过低    -> "可疑(疑似英文垃圾输出)"
    * 其余                          -> "正常"
- 所有打印写入 fastgpt_push/test_llm_output.log，不影响 push_fastgpt.log。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path

# 便于以脚本方式运行时也能 import 父级 find_similar_jira 的模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from utils.logger import Logger, log as mylog
from utils.llm_client import build_llm_client

# ============================ 判定阈值 ============================
# 疑似垃圾：长度超过该值且中文字符占比低于该比例的文本
SUSPICIOUS_MIN_LEN = 500
SUSPICIOUS_MIN_CHINESE_RATIO = 0.2

# ============================ 探测任务定义 ============================
# 每类任务：system 提示词 + user 问题 + 期望关键词/是否必须 JSON
PROBES: dict[str, dict] = {
    "json": {
        "system": (
            "你是一名严谨的 JSON 输出助手。只输出 JSON，禁止输出 JSON 以外的任何文字、"
            "注释或解释。格式严格如下：\n"
            '{"problem_causes": ["问题原因1", "问题原因2"]}\n'
        ),
        "user": "某设备播放 Dolby Vision 流时主屏画面颜色异常且闪屏，HDMITX 画面静帧，"
                "请分析导致问题的原因并严格按 JSON 格式输出。",
        "mode": "json",
        "expected_keyword": "problem_causes",
    },
    "summary": {
        "system": (
            "你是一名专业的问题总结助手。请按以下三部分输出，各部分单独一行：\n"
            "问题总结：一句话\n"
            "问题现象：一句话\n"
            "具体复现步骤：Step1/Step2/Step3\n"
        ),
        "user": "某测试设备在执行 test_monkey 时发生 kernel_panic，系统直接崩溃。",
        "mode": "text",
        "expected_keyword": "问题总结",
    },
}


def _chinese_ratio(text: str) -> float:
    """统计文本中中文字符占比（0~1）。"""
    total = max(len(text), 1)
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return chinese / total


def classify(text: str, probe: dict) -> tuple[str, str]:
    """对一次 LLM 输出做质量判定，返回 (判定结果, 说明)。

    判定结果取值：正常 / 不可用(非JSON) / 不可用(缺关键词) /
                  可疑(疑似英文垃圾输出) / 调用失败。
    """
    length = len(text)
    ratio = _chinese_ratio(text)
    mode = probe.get("mode")
    keyword = probe.get("expected_keyword", "")

    if mode == "json":
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return "不可用(非JSON)", f"json.loads 失败：{exc}"
        if keyword and not isinstance(parsed, dict):
            return "不可用(非JSON)", f"JSON 顶层不是对象，实际类型 {type(parsed).__name__}"
        if keyword and keyword not in parsed:
            return "不可用(缺关键词)", f"JSON 中缺少字段 {keyword!r}，现有字段 {list(parsed.keys())}"
        detail = f"JSON 解析成功，字段 {list(parsed.keys())}，长度 {length} 字符"
        return "正常", detail

    # 文本类任务
    if keyword and keyword not in text:
        return "不可用(缺关键词)", f"输出中未出现目标关键词 {keyword!r}，长度 {length} 字符，中文占比 {ratio:.1%}"
    if length > SUSPICIOUS_MIN_LEN and ratio < SUSPICIOUS_MIN_CHINESE_RATIO:
        return "可疑(疑似英文垃圾输出)", f"长度 {length} 字符，中文占比仅 {ratio:.1%}"
    return "正常", f"长度 {length} 字符，中文占比 {ratio:.1%}"


def _qa_with_timeout(llm_client, system_prompt: str, user_prompt: str, timeout: float):
    """在线程中调用 LLM，超过 timeout 秒则放弃本次调用，防止服务挂起导致测试卡死。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        llm_client.qa_with_system,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        return None  # 表示调用超时
    except BaseException:
        executor.shutdown(wait=True)
        raise
    else:
        executor.shutdown(wait=True)


def run_probe(llm_client, probe_name: str, probe: dict, timeout: float) -> tuple[str, str, float]:
    """执行一次 LLM 探测，返回 (判定结果, 说明, 耗时秒)。

    不重试：异常/超时也如实返回"调用失败"。
    """
    start = time.perf_counter()
    mylog(f"[测试] {probe_name} 探测开始：{probe['user'][:40]}...（超时上限 {timeout} 秒）")
    text = None
    try:
        text = _qa_with_timeout(
            llm_client,
            system_prompt=probe["system"],
            user_prompt=probe["user"],
            timeout=timeout,
        )
        if text is None:
            elapsed = round(time.perf_counter() - start, 1)
            detail = f"调用超时：超过 {timeout} 秒未返回，已放弃本次调用"
            mylog(f"[测试] {probe_name} 探测超时：{detail}")
            return "调用失败", detail, elapsed
    except Exception as exc:
        elapsed = round(time.perf_counter() - start, 1)
        detail = f"{type(exc).__name__}: {exc}"
        mylog(f"[测试] {probe_name} 探测调用异常：{detail}")
        return "调用失败", detail, elapsed

    elapsed = round(time.perf_counter() - start, 1)
    verdict, detail = classify(text, probe)
    preview = text[:80].replace("\n", " ")
    mylog(
        f"[测试] {probe_name} 探测完成：判定={verdict}，耗时 {elapsed} 秒，"
        f"长度 {len(text)} 字符，中文占比 {_chinese_ratio(text):.1%}"
    )
    mylog(f"[测试] {probe_name} 输出前 80 字符：{preview}")
    if verdict == "正常":
        mylog(f"[测试] {probe_name} 说明：{detail}")
    else:
        mylog(f"[测试] {probe_name} 异常说明：{detail}")
    return verdict, detail, elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 输出质量诊断测试")
    parser.add_argument("--count", type=int, default=3, help="每类探测重复次数（默认 3）")
    parser.add_argument("--interval", type=float, default=5.0, help="每次探测之间的间隔秒数（默认 5）")
    parser.add_argument(
        "--type",
        default="all",
        choices=["all", "json", "summary"],
        help="探测类型：all/json/summary（默认 all）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="单次 LLM 调用超时秒数，超过则放弃并记为调用失败（默认 600，实测单次约 8 分钟）",
    )
    args = parser.parse_args()

    # 用独立日志文件，避免与 push_fastgpt.log 混写
    Logger.init(str(Path(__file__).resolve().parent / "test_llm_output.log"))
    mylog(
        f"[配置] 测试日志文件：test_llm_output.log；每类探测 {args.count} 次，"
        f"间隔 {args.interval} 秒，单次超时 {args.timeout} 秒"
    )

    probes = PROBES if args.type == "all" else {args.type: PROBES[args.type]}
    mylog(f"[配置] 待探测类型：{list(probes.keys())}；LLM 预设：{config.LLM_PRESET}")

    llm_client = build_llm_client(preset_name=config.LLM_PRESET)
    mylog("[配置] LLM 客户端构建完成，开始探测")

    summary: dict[str, dict] = {}   # probe_name -> {正常/不可用/可疑/调用失败: 计数}
    total_elapsed = 0.0

    for probe_name, probe in probes.items():
        counts = {"正常": 0, "不可用(非JSON)": 0, "不可用(缺关键词)": 0,
                  "可疑(疑似英文垃圾输出)": 0, "调用失败": 0}
        for i in range(1, args.count + 1):
            mylog(f"===== 第 {i}/{args.count} 次【{probe_name}】探测 =====")
            verdict, _detail, elapsed = run_probe(llm_client, probe_name, probe, args.timeout)
            counts[verdict] = counts.get(verdict, 0) + 1
            total_elapsed += elapsed
            if i < args.count and args.interval > 0:
                mylog(f"[测试] 间隔 {args.interval} 秒后进行下一次探测...")
                time.sleep(args.interval)
        summary[probe_name] = counts

    # ---- 汇总结论 ----
    mylog("===== 汇总结果 =====")
    all_bad = 0
    all_good = 0
    for probe_name, counts in summary.items():
        bad = counts["不可用(非JSON)"] + counts["不可用(缺关键词)"] + counts["调用失败"]
        suspicious = counts["可疑(疑似英文垃圾输出)"]
        good = counts["正常"]
        all_bad += bad
        all_good += good
        mylog(
            f"[汇总] {probe_name}: 正常 {good} / 可疑(疑似垃圾) {suspicious} / "
            f"不可用或调用失败 {bad}"
        )
    mylog(f"[统计] 共探测 {args.count * len(probes)} 次，总耗时 {total_elapsed:.1f} 秒，判定 正常 {all_good} 次")
    if all_bad or any(counts["可疑(疑似英文垃圾输出)"] for counts in summary.values()):
        mylog("[结论] 检测到 LLM 服务输出异常（垃圾/不可用/失败），建议检查 ollama 服务状态与模型预设")
    else:
        mylog("[结论] 本次探测未发现异常，LLM 服务输出正常")


if __name__ == "__main__":
    main()