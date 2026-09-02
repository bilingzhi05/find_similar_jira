"""
入口模块：向 fastgpt 知识库推送 JIRA 数据。

用法示例：
    python fastgpt_push/main.py OTT-80575 SWPL-272411
    python fastgpt_push/main.py --dry-run OTT-80575
    python fastgpt_push/main.py --timeout 120 OTT-80575

日志规则：
- 本程序所有打印（过程/成功/失败）统一写入 fastgpt_push/push_fastgpt.log，
  该文件与 find_similar_jira 的 tool_execution.log 相互隔离，互不写入；
- 文件内按"成功列表 / 失败明细 / 汇总"分节呈现，互不混合。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让 fastgpt_push 能 import 父级 find_similar_jira 的 utils / src 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from utils.logger import Logger, log as mylog
from jira_fields import JiraDataProvider
from data_builder import build_q_a
from fastgpt_api import push_to_fastgpt
from retry import RetryFailedError


def parse_args() -> argparse.Namespace:
    """解析命令行参数：jira_keys...（支持多个）、--dry-run、--timeout。"""
    parser = argparse.ArgumentParser(description="向 fastgpt 知识库推送 JIRA 数据")
    parser.add_argument("jira_keys", nargs="+", help="一个或多个 JIRA key，例如 OTT-80575")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只构建 json_q/json_a 并打印预览，不真正向 fastgpt 推送",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="覆盖 fastgpt 推送超时时间（秒）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 初始化独立日志文件：本程序所有打印都写到这里，与 find_similar_jira 的 tool_execution.log 隔离
    Logger.init(config.LOGGER_FILE)
    mylog(f"[配置] 日志文件：{config.LOGGER_FILE}")

    # jira_manager 内部创建客户端时从环境变量读取凭据，这里用配置兜底
    os.environ.setdefault("JIRA_USERNAME", config.JIRA_USERNAME)
    os.environ.setdefault("JIRA_PASSWORD", config.JIRA_PASSWORD)

    if args.timeout:
        config.FASTGPT_TIMEOUT = args.timeout
        mylog(f"[配置] 用户指定 fastgpt 推送超时：{args.timeout} 秒")

    prompts = config.load_prompts()
    mylog(
        f"[配置] LLM 预设={config.LLM_PRESET}；提示词键={sorted(prompts.keys())}；"
        f"重试次数={config.RETRY_TIMES}，间隔={config.RETRY_INTERVAL}秒"
    )
    if args.dry_run:
        mylog("[配置] 当前为 --dry-run 模式：只构建并打印，不推送")

    provider = JiraDataProvider(config.JIRA_SERVER, config.JIRA_USERNAME, config.JIRA_PASSWORD)
    mylog(f"[配置] jira 客户端已就绪，服务器={config.JIRA_SERVER}")

    success_items: list[str] = []     # 成功构建/push 的 key
    failed_items: list[tuple] = []    # 失败明细：(key, 完整错误链文本)

    for jira_id in args.jira_keys:
        jira_id = str(jira_id).strip()
        mylog(f"===== 开始处理 {jira_id} =====")
        try:
            json_q, json_a = build_q_a(jira_id, prompts, provider)
            if args.dry_run:
                mylog(f"[dry-run] {jira_id} 不推送，json_q 预览（前100字符）：{json_q[:100]}")
                mylog(f"[dry-run] {jira_id} 不推送，json_a 预览（前300字符）：{json_a[:300]}")
                success_items.append(jira_id)
                continue
            push_to_fastgpt(json_q, json_a)
            mylog(f"[推送] {jira_id} 已成功写入 fastgpt 知识库")
            success_items.append(jira_id)
        except RetryFailedError as exc:
            reason = exc.format_error_chain()
            mylog(f"[失败] {jira_id} 多次重试后仍失败，未写入知识库：\n{reason}")
            failed_items.append((jira_id, reason))
        except Exception as exc:
            reason = f"未捕获异常：{type(exc).__name__}: {exc}"
            mylog(f"[失败] {jira_id} 处理异常，未写入知识库：{reason}")
            failed_items.append((jira_id, reason))

    # ---- 分节汇总（成功/失败分开呈现，互不混合）----
    mylog("===== 成功列表 =====")
    if success_items:
        for key in success_items:
            mylog(f"  - {key}")
    else:
        mylog("  （无）")

    mylog("===== 失败明细（未入知识库）=====")
    if failed_items:
        for key, reason in failed_items:
            mylog(f"  >>> {key}")
            for line in reason.splitlines():
                mylog(f"      {line}")
    else:
        mylog("  （无）")

    mylog(f"===== 汇总：成功 {len(success_items)} / 失败 {len(failed_items)} =====")

    if failed_items:
        sys.exit(1)


if __name__ == "__main__":
    main()