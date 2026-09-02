"""
JIRA 扩展读取：在 MyJira 基础上补充 fastgpt 推送所需字段的读取方法。

新增字段：
- chip / components / key_error_logs / patch_list / doc_link / source；
- rd_manager（复用 src/jira_manager 的优先级解析逻辑）。

说明：
- 各字段未在 JIRA 上配置时返回空值（空字符串 / 空列表），这是正常兜底，不算失败；
- 真正由网络/服务端异常导致的读取失败会让异常向上传播，由外层重试机制处理，
  多次重试仍失败则该条 JIRA 不入库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 便于以脚本方式运行时也能 import 父级 find_similar_jira 的模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# jira_manager 在模块导入阶段会读取环境变量 JIRA_USERNAME / JIRA_PASSWORD，
# 这里提前用配置兜底，保证其内部创建的 jira 客户端有凭据可用。
import config as cfg
from utils.logger import log as mylog
from utils.jira_client import MyJira
from src.processor import extract_key_fields
from src.jira_manager import get_manager_by_jira_id


class JiraDataProvider(MyJira):
    """在 MyJira 基础上扩展 fastgpt 推送所需字段的读取。"""

    # ---------------- 字符串字段 ----------------
    def getChip(self, issue_key: str) -> str:
        """读取芯片字段（chip），缺失返回空字符串。"""
        issue = self.mJira.issue(issue_key)
        value = self._get_field_value_by_names(issue, cfg.FIELD_ALIASES["chip"])
        return self._to_str(value)

    def getComponents(self, issue_key: str) -> str:
        """读取组件字段：优先 issue.fields.components 名称拼接，回退候选字段名。"""
        issue = self.mJira.issue(issue_key)
        components = getattr(getattr(issue, "fields", None), "components", None)
        if components:
            names = [c.name for c in components if getattr(c, "name", None)]
            if names:
                text = ", ".join(names)
                mylog(f"[jira_fields] {issue_key} 组件（issue.fields.components）：{text}")
                return text
        return self._to_str(self._get_field_value_by_names(issue, cfg.FIELD_ALIASES["components"]))

    def getKeyErrorLogs(self, issue_key: str, cleaned_description: str) -> str:
        """读取关键错误日志字段；字段缺失时从清洗后的 description 提取"错误日志"段落。"""
        issue = self.mJira.issue(issue_key)
        value = self._get_field_value_by_names(issue, cfg.FIELD_ALIASES["key_error_logs"])
        if value:
            return self._to_str(value)
        fallback = extract_key_fields(cleaned_description or "").get("error_logs", "")
        if fallback:
            mylog(f"[jira_fields] {issue_key} 未配置关键日志字段，改用 description 中提取的错误日志（{len(fallback)}字符）")
        return fallback

    def getSource(self, issue_key: str) -> str:
        """读取来源字段（source），缺失返回空字符串。"""
        issue = self.mJira.issue(issue_key)
        return self._to_str(self._get_field_value_by_names(issue, cfg.FIELD_ALIASES["source"]))

    # ---------------- 列表字段 ----------------
    def getPatchList(self, issue_key: str) -> list:
        """读取补丁列表字段（patch_list），归一化为 list[str]。"""
        issue = self.mJira.issue(issue_key)
        value = self._get_field_value_by_names(issue, cfg.FIELD_ALIASES["patch_list"])
        return self._to_list(value)

    def getDocLink(self, issue_key: str) -> list:
        """读取文档链接字段（doc_link），归一化为 list[str]。"""
        issue = self.mJira.issue(issue_key)
        value = self._get_field_value_by_names(issue, cfg.FIELD_ALIASES["doc_link"])
        return self._to_list(value)

    # ---------------- rd_manager ----------------
    def getRdManager(self, issue_key: str) -> str:
        """解析该 JIRA 的 rd_manager（复用 src/jira_manager 的优先级规则与保底策略）。"""
        manager, priority = get_manager_by_jira_id(issue_key)
        mylog(f"[jira_fields] {issue_key} rd_manager={manager or '<空>'}（{priority}）")
        return manager

    # ---------------- 类型归一化 ----------------
    @staticmethod
    def _to_str(value) -> str:
        """把 JIRA 字段值归一化为字符串；数组/对象取其中的名称文本。"""
        if value is None:
            return ""
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    part = item.get("value") or item.get("name") or item.get("url") or ""
                else:
                    part = item
                if str(part).strip():
                    parts.append(str(part).strip())
            return ", ".join(parts)
        if isinstance(value, dict):
            return str(value.get("value") or value.get("name") or value.get("url") or "").strip()
        return str(value).strip()

    @staticmethod
    def _to_list(value) -> list:
        """把 JIRA 字段值归一化为 list[str]；字符串按换行/逗号拆分。"""
        if value is None:
            return []
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict):
                    part = item.get("url") or item.get("name") or item.get("value") or ""
                else:
                    part = item
                if str(part).strip():
                    result.append(str(part).strip())
            return result
        if isinstance(value, str):
            return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
        return [str(value).strip()] if str(value).strip() else []