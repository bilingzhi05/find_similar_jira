"""
配置模块：集中管理 fastgpt 推送程序的全部可调参数。

取值优先级：
- 网络凭据/超时等支持环境变量覆盖，未设置时使用本文件默认值；
- LLM 提示词优先加载父级 find_similar_jira/config.json 的 pipeline.prompts，
  未命中时回落为与 src/pipeline.py 相同的内联默认提示词。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 便于以脚本方式运行时也能 import 父级 find_similar_jira 的 utils 模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import log as mylog

# ============================ JIRA 基础配置 ============================
# 环境变量优先，回落为与现有 pipeline 相同的取值
JIRA_SERVER = os.getenv("JIRA_SERVER") or "https://jira.amlogic.com"
JIRA_USERNAME = os.getenv("JIRA_USERNAME") or "lingzhi.bi"
JIRA_PASSWORD = os.getenv("JIRA_PASSWORD") or "Qwer!234567"

# ============================ fastgpt 推送配置 ============================
FASTGPT_URL = "http://10.18.11.98:3000/api/core/dataset/data/pushData"
# 环境变量 FASTGPT_API_KEY 优先，回落为授权 token
FASTGPT_TOKEN = os.getenv("FASTGPT_API_KEY") or (
    "fastgpt-xxVOCWkOre8Q1szSLicBgmqhyuvfO5A9nbLk40rsjIVqmRSdMhMOUw51"
)
COLLECTION_ID = "6a8e908848ac7d19447aeddc"  # fastgpt 数据集 id
TRAINING_TYPE = "chunk"                      # 训练类型：chunk 模式
BILL_ID = ""                                 # 可选：训练订单聚合 id，为空时不上送该字段

# ============================ 超时与重试 ============================
FASTGPT_TIMEOUT = 60   # fastgpt 推送请求超时（秒），可通过命令行 --timeout 覆盖
RETRY_TIMES = 3        # 失败最大重试次数（含首次）
RETRY_INTERVAL = 2     # 每次重试前的等待秒数
MAX_TOKEN = 2096       # 评论分块总结的单块 token 上限，与 pipeline 默认一致

# ============================ LLM 配置 ============================
LLM_PRESET = "ollama_qwen3_4b_fp16"  # 读取父级 config.json 的 llm_presets

# ============================ 日志相关 ============================
# 本程序唯一的日志文件：过程/成功/失败打印全部写入此文件，
# 与 find_similar_jira 的 tool_execution.log 相互隔离。
LOGGER_FILE = str(Path(__file__).resolve().parent / "push_fastgpt.log")

# ============================ 新增字段候选名 ============================
# JIRA 上的 customfield 名称在不同项目里可能有差异，这里配置候选名列表，
# 命中第一个非空即用；全部未命中则按空值/空列表兜底，程序不中断。
FIELD_ALIASES: dict[str, list[str]] = {
    "chip": ["chip", "Chip", "芯片"],
    "components": ["components", "Components", "组件"],
    "key_error_logs": ["key_error_logs", "Key Error Logs", "关键错误日志"],
    "patch_list": ["patch list", "Patch List", "patch_list", "补丁", "补丁列表"],
    "doc_link": ["doc link", "Doc Link", "doc_link", "文档链接", "Document"],
    "source": ["source", "Source", "来源"],
}

# ============================ 提示词 ============================
_PARENT_CONFIG = str(Path(__file__).resolve().parents[1] / "config.json")

_PROMPT_DEFAULTS: dict[str, str] = {
    "summary_system": """## 角色：
你是一名专业的问题总结助手。

## 任务：
根据提供的信息，严格生成以下内容，禁止输出其他无关信息：

1. **问题总结**：  
- 用一句话概括问题发生的操作和结果 
- **仅当涉及软件版本时需进行模糊化**（如具体 ROM 版本号、系统版本号） 
- 软件版本统一替换为：**"某版本"** 
- 设备型号、测试名称、异常类型等其他专有名词可正常保留

2. **问题现象**：  
- 简洁描述实际观察到的异常表现 
- 可保留具体技术名词、测试名称、异常类型及设备信息 
- **不对软件版本以外的信息做模糊处理**

3. **具体复现步骤**：  
- 使用 Step1、Step2、Step3 格式详细列出复现流程 
- 包含关键操作、环境、版本或依赖条件 
- **步骤中若出现软件版本，仅替换为"某版本"** 


## 输出要求：
- 每个字段独立清晰，不允许合并或遗漏  
- 严禁添加"注：""备注："或其他多余文字  
- 使用 Markdown 或文本格式均可  
- 保持简洁、专业、易读
- **禁止出现具体的专有名称**（例如频道编号、确切文件名、设备型号、应用名称等），用"某频道""某设备""某片源"等替代。

## 示例格式说明（仅作演示，不可直接使用）：

问题总结：在某版本系统环境下执行 test_monkey 自动化测试时，触发 kernel_panic 导致测试中断 
问题现象：执行 test_monkey 过程中发生 kernel_panic，系统直接崩溃 
具体复现步骤： 
Step1：在某版本 Rom 环境下启动 test_monkey 测试 
Step2：执行自动化测试流程并模拟用户操作 
Step3：测试过程中系统触发 kernel_panic，测试流程终止
""",
    "comments_summary_user_prefix": "帮我总结以下内容：",
    "comments_summary_system": """## 角色
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
无线非裁剪音轨切换烤机（钟卫工位）在27日测试过程中出现黑屏一直在加载，日志显示"sendWatchLiveChannel: WARN: http error code = 404. [PERF] 498ms, url='https://api.claro.com.br/residential/v1/userusages/contents'"，播放501 dash时黑屏，且App访问license接口也返回404；无线裁剪音轨切换烤机（机顶盒2）在27日测试过程中同样出现黑屏一直在加载，日志显示有OOM导致IP-9。
""",
    "comments_extract_system": """## 角色
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
- 如需指代，可使用"某频道""某片源""某配置""某功能模块""相关人员"等抽象替代描述。

## 请严格按以下输出格式输出：

1、复现方式与现象:一句话总结（带关键细节），如缺失则返回空

2、定位结果:一句话总结（带关键细节），如缺失则返回空

3、解决方案:一句话总结（带关键细节），如缺失则返回空
""",
    "root_cause_system": """### 角色设定
你是一名资深 Android / 多媒体系统问题分析专家，擅长从 JIRA 的  
`issue_description` 和 `comments` 中抽取**真正导致问题发生的根因（problem causes）**，用于问题相似度匹配与归类。

---

### 任务目标
根据输入的 JIRA 内容，分析**是什么原因导致问题**，输出可直接用于相似度计算的问题点列表。

---

### 严格分析规则
1. 每条只保留 **直接导致问题的根因**
2. 回答"问题是因为什么原因而发生的"，
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
- 必须能单独回答："这个问题为什么会发生？"
- 禁止输出名词、短语或关键词堆砌
- 错误示例："内存越界""DDR位翻转""buffer manager 初始化失败"
- 正确示例："由于 buffer manager 初始化失败，内核在后续访问中触发空指针解引用并导致 kernel panic"
8. 简洁明了，一句话就能说明白
9. 输出必须严格遵守 JSON 格式：

{
"problem_causes": [
    "问题原因 1",
    "问题原因 2"
]
}
""",
}


def _load_parent_prompts() -> dict:
    """读取父级 config.json 的 pipeline.prompts；读取失败返回空 dict。"""
    try:
        data = json.loads(Path(_PARENT_CONFIG).read_text(encoding="utf-8"))
        return data.get("pipeline", {}).get("prompts", {})
    except Exception as exc:
        mylog(f"[配置] 读取父级 config.json 失败，将使用内联默认提示词：{exc}")
        return {}


def load_prompts() -> dict:
    """返回合并后的提示词 dict：父级配置优先，缺项回落到内联默认。"""
    merged: dict[str, str] = {}
    merged.update(_PROMPT_DEFAULTS)
    merged.update(_load_parent_prompts())
    return merged