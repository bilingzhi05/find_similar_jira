import requests
from typing import Dict, Tuple

import re
import urllib3
from jira import JIRA

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class MyJira:
    
    def __init__(self, jiraserver, username, password):
        self.mLogin_options = {"verify": False}
        self.mJiraServer = jiraserver
        self.mUserName = username
        self.mPassword = password
        self.build_jira()
        self.components_array = set()
        self._field_name_map = None

    def build_jira(self):
        self.mJira = JIRA(self.mJiraServer, options=self.mLogin_options, basic_auth=(self.mUserName, self.mPassword))  # 创建jira连接

    def _get_field_name_map(self):
        if self._field_name_map is not None:
            return self._field_name_map
        fields = self.mJira.fields()
        mapping = {}
        for field in fields:
            name = (field.get("name") or "").lower()
            field_id = field.get("id")
            if name and field_id:
                mapping[name] = field_id
        self._field_name_map = mapping
        return mapping

    def _get_field_value_by_names(self, issue, names):
        fields = issue.raw.get("fields", {})
        mapping = self._get_field_name_map()
        for name in names:
            field_id = mapping.get(name.lower())
            if field_id and field_id in fields:
                return fields.get(field_id)
        return None

    def getBugAttachments(self, issue, patern, component_name):
        """
            :param issue_id: issue_id
            :return: 保存所有附件，如果没有附件则提示信息
        """
        fields = self.mJira.issue(id=issue.id, expand="summary").fields
        summary = fields.summary
        if len(summary) > 128:
            summary = summary[:128]
        print(f'summary:{"".join(summary)}')
        fields = self.mJira.issue(id=issue.id, expand="attachment").fields
        attachments = fields.attachment
        if len(attachments) != 0:
            need_dealwith = []
            no_need_dealwith =[]
            for i in range(len(attachments)):
                file_name = f"{attachments[i].filename}"
                if not re.match(patern, file_name):
                    no_need_dealwith.append(file_name)
                    continue
                need_dealwith.append(file_name)
                path = f"{''+issue.id+'_'+''.join(map(str,summary))+'_'+file_name}"
                path = path.replace(':','_').replace('/',"_")
                path = component_name+'/'+path
                with open(path, "wb") as f:
                    f.write(attachments[i].get())
            print(f'issuse id:{issue.id}, no need to deal with attachments of:{no_need_dealwith}\n')
            print(f'need to deal with attachments of:{need_dealwith}\n')
            print(f'--------->issue_id:{issue.id} attachment download finished!')
        else:
            print("没有附件")

    def getEarliestAttachmentTime(self, issue, patern):
        fields = self.mJira.issue(id=issue.id, expand="attachment").fields
        attachments = fields.attachment
        if not attachments:
            return None
        created_times = []
        if len(attachments) != 0:
            need_dealwith = []
            no_need_dealwith =[]
            for att in attachments:
                file_name = f"{att.filename}"
                if not re.match(patern, file_name):
                    no_need_dealwith.append(file_name)
                    continue
                if getattr(att, "created", None):
                    need_dealwith.append(file_name)
                    created_times.append(att.created)
        if not created_times:
            return None
        return min(created_times)

    def getPriorityHighFirstTime(self, issue):
        issue = self.mJira.issue(issue.key, expand="changelog")

        histories = getattr(getattr(issue, "changelog", None), "histories", [])
        applied_times = []
        # print(f'histories:{histories}')
        for history in histories:
            for item in history.items:
                if item.field != "priority":
                    continue
                to_string = (getattr(item, "toString", "") or "").lower()
                to_value = (getattr(item, "to", "") or "").lower()
                if to_string in {"high", "highest"} or to_value in {"high", "highest"}:
                    applied_times.append(history.created)
        if not applied_times:
            fields = getattr(issue, "fields", None)
            priority_name = getattr(getattr(fields, "priority", None), "name", None)
            created_time = getattr(fields, "created", None)
            if priority_name and priority_name.lower() in {"high", "highest", "p1", "p0"} and created_time:
                return created_time
            return None
        return min(applied_times)

    def getAllComponents(self):
        for project in self.mJira.projects():
            components = self.mJira.project_components(project)
            component_names = [component.name for i, component in enumerate(components)]
            for component_name in component_names:
                self.components_array.add(component_name)
    # if len(Component) != 0:
    #     self.components_array.add(set(Component))
    def getJiraStatus(self, key):
        """
            :param issue_id: issue_id
            :return: 返回jira状态
        """
        sql = f"key = {key}"
        try:
            issues = self.search_issues(sql)
        except Exception:
            return "ERROR"
        if not issues:
            return "ERROR"
        for issue in issues:
            status_name = issue.fields.status.name
        return status_name

    def show_the_commponents(self):
        print(f'------->{self.components_array}')
    
    def search_issues(self, jql, maxResults=99999):
        """包装JIRA的search_issues方法"""
        return self.mJira.search_issues(jql, maxResults=maxResults)

    def getJiraLenWithTime(self, jql, maxResults=99999):
        issues = self.search_issues(jql, maxResults=maxResults)
        key_time_list = []
        for issue in issues:
            issue_time = getattr(getattr(issue, "fields", None), "created", None)
            key_time_list.append({"key":issue.key, "create_time":issue_time})
        return key_time_list
    
    def get_issue_keys(self, jql: str):
        issues = self.search_issues(jql)
        return [issue.key for issue in issues]
    
    def getJiraLen(self, jql, maxResults=99999):
        issues = self.search_issues(jql, maxResults=maxResults)
        return len(issues)
    
    def getLabelAppliedTime(self, issue_key, label):
        issue = self.mJira.issue(issue_key, expand="changelog")
        histories = getattr(getattr(issue, "changelog", None), "histories", [])
        applied_times = []
        for history in histories:
            for item in history.items:
                if item.field != "labels":
                    continue
                to_string = getattr(item, "toString", "") or ""
                from_string = getattr(item, "fromString", "") or ""
                to_value = getattr(item, "to", "") or ""
                from_value = getattr(item, "from", "") or ""
                if label in to_string and label not in from_string or label in str(to_value) and label not in str(from_value):
                    applied_times.append(history.created)
        if not applied_times:
            keyword = "AI智能分析"
            applied_time = self.getAiCommentTime(issue_key, keyword)
            if applied_time:
                return applied_time
            return None
        return min(applied_times)
    
    def getAiCommentTime(self, issue_key, keyword="AI智能分析"):
        """
        获取issue中第一条包含指定关键字的评论时间
        :param issue_key: JIRA issue key
        :param keyword: 要搜索的关键字，默认为"AI智能分析"
        :return: 第一条匹配评论的创建时间，如果没有匹配则返回None
        """
        issue = self.mJira.issue(issue_key, expand="comments")
        comments = getattr(getattr(issue, "fields", None), "comment", None)
        if not comments or not getattr(comments, "comments", None):
            return None
        
        for comment in comments.comments:
            body = getattr(comment, "body", "") or ""
            if keyword in body:
                return getattr(comment, "created", None)
        return None

    def getAiCommentTimeWithSql(self, sql, keyword="AI智能分析"):
        """
        批量获取多个issue中第一条包含指定关键字的评论时间
        :param sql: JQL查询语句
        :param keyword: 要搜索的关键字，默认为"AI智能分析"
        :return: 包含issue key和评论时间的列表
        """
        issues = self.search_issues(sql)
        comment_time = []
        for issue in issues:
            applied_time = self.getAiCommentTime(issue.key, keyword)
            if applied_time:
                comment_time.append({"key": issue.key, "ai_comment_time": applied_time})
        return comment_time

    def getComments(self, issue_key):
        """
        获取issue的评论内容
        :param issue_key: JIRA issue key
        :return: 评论内容列表
        """
        issue = self.mJira.issue(issue_key, expand="comments")
        comments = getattr(getattr(issue, "fields", None), "comment", None)
        if not comments or not getattr(comments, "comments", None):
            return []
        return [getattr(comment, "body", "") or "" for comment in comments.comments]

    def getCommentsWithSql(self, sql):
        """
        批量获取多个issue的评论内容
        :param sql: JQL查询语句
        :return: 包含issue key和评论内容列表的结果
        """
        issues = self.search_issues(sql)
        comment_list = []
        for issue in issues:
            comments = self.getComments(issue.key)
            if comments:
                comment_list.append({"key": issue.key, "comments": comments})
        return comment_list

    def getSummary(self, issue_key):
        """
        获取issue的summary
        :param issue_key: JIRA issue key
        :return: summary内容
        """
        issue = self.mJira.issue(issue_key, expand="summary")
        return getattr(getattr(issue, "fields", None), "summary", None)

    def getSummaryWithSql(self, sql):
        """
        批量获取多个issue的summary
        :param sql: JQL查询语句
        :return: 包含issue key和summary的结果
        """
        issues = self.search_issues(sql)
        summary_list = []
        for issue in issues:
            summary = self.getSummary(issue.key)
            if summary:
                summary_list.append({"key": issue.key, "summary": summary})
        return summary_list

    def getDescription(self, issue_key):
        """
        获取issue的description
        :param issue_key: JIRA issue key
        :return: description内容
        """
        issue = self.mJira.issue(issue_key, expand="description")
        return getattr(getattr(issue, "fields", None), "description", None)

    def getDescriptionWithSql(self, sql):
        """
        批量获取多个issue的description
        :param sql: JQL查询语句
        :return: 包含issue key和description的结果
        """
        issues = self.search_issues(sql)
        description_list = []
        for issue in issues:
            description = self.getDescription(issue.key)
            if description:
                description_list.append({"key": issue.key, "description": description})
        return description_list

    def getRootCause(self, issue_key):
        """
        获取issue的root_cause
        :param issue_key: JIRA issue key
        :return: root_cause内容
        """
        issue = self.mJira.issue(issue_key)
        return self._get_field_value_by_names(issue, ["root_cause", "root cause", "Root Cause"])

    def getRootCauseWithSql(self, sql):
        """
        批量获取多个issue的root_cause
        :param sql: JQL查询语句
        :return: 包含issue key和root_cause的结果
        """
        issues = self.search_issues(sql)
        root_cause_list = []
        for issue in issues:
            root_cause = self.getRootCause(issue.key)
            if root_cause:
                root_cause_list.append({"key": issue.key, "root_cause": root_cause})
        return root_cause_list

    def getHowToFix(self, issue_key):
        """
        获取issue的how_to_fix
        :param issue_key: JIRA issue key
        :return: how_to_fix内容
        """
        issue = self.mJira.issue(issue_key)
        return self._get_field_value_by_names(issue, ["how_to_fix", "how to fix", "How To Fix"])

    def getHowToFixWithSql(self, sql):
        """
        批量获取多个issue的how_to_fix
        :param sql: JQL查询语句
        :return: 包含issue key和how_to_fix的结果
        """
        issues = self.search_issues(sql)
        how_to_fix_list = []
        for issue in issues:
            how_to_fix = self.getHowToFix(issue.key)
            if how_to_fix:
                how_to_fix_list.append({"key": issue.key, "how_to_fix": how_to_fix})
        return how_to_fix_list

    def getSoftwareRelease(self, issue_key):
        """
        获取issue的Software Release
        :param issue_key: JIRA issue key
        :return: Software Release内容
        """
        issue = self.mJira.issue(issue_key)
        software_release = self._get_field_value_by_names(issue, ["Software Release", "software release"])
        if software_release is None:
            return ""
        return self.pick_value(software_release)

    def getSoftwareReleaseWithSql(self, sql):
        """
        批量获取多个issue的Software Release
        :param sql: JQL查询语句
        :return: 包含issue key和Software Release的结果
        """
        issues = self.search_issues(sql)
        release_list = []
        for issue in issues:
            release_value = self.getSoftwareRelease(issue.key)
            if release_value:
                release_list.append({"key": issue.key, "software_release": release_value})
        return release_list

    def getProjectId(self, issue_key):
        """
        获取issue的Project ID
        :param issue_key: JIRA issue key
        :return: Project ID内容
        """
        issue = self.mJira.issue(issue_key)
        project = self._get_field_value_by_names(issue, ["Project ID", "project id"])
        if project is not None:
            return self.pick_value(project)
        project_field = getattr(getattr(issue, "fields", None), "project", None)
        return self.pick_value(project_field)

    def pick_value(self, field):
        if isinstance(field, list):
            return ", ".join(
                str(item.get("value") or item.get("name") or "")
                for item in field
                if isinstance(item, dict)
            ).strip(", ")
        if isinstance(field, dict):
            return field.get("value") or field.get("name") or ""
        return str(field or "")

    def getProjectIdWithSql(self, sql):
        """
        批量获取多个issue的Project ID
        :param sql: JQL查询语句
        :return: 包含issue key和Project ID的结果
        """
        issues = self.search_issues(sql)
        project_list = []
        for issue in issues:
            project_id = self.getProjectId(issue.key)
            if project_id:
                project_list.append({"key": issue.key, "project_id": project_id})
        return project_list
    
    def getLabelAppliedTimeWithSql(self, sql, label):
        issues = self.search_issues(sql)
        label_time = []

        for issue in issues:
            applied_time = self.getLabelAppliedTime(issue.key, label)
            if applied_time:
                label_time.append({"key":issue.key, "label_applied_time":applied_time})

        return label_time

    def getPriorityHighFirstTimeWithSql(self, sql):
        issues = self.search_issues(sql)
        priority_time = []
        for issue in issues:
            applied_time = self.getPriorityHighFirstTime(issue)
            if applied_time:
                priority_time.append({"key":issue.key, "priority_high_time":applied_time})
        return priority_time

    def getEarliestAttachmentTimeWithSql(self, sql, patern=r".*\.(log|txt|zip|rar|7z|xz|gz|tar)$"):
        issues = self.search_issues(sql)
        attachment_time = []
        print(f"len(issues):{len(issues)}")
        for issue in issues:
            earliest_time = self.getEarliestAttachmentTime(issue, patern)
            if earliest_time:
                attachment_time.append({"key":issue.key, "attachment_time":earliest_time})

        return attachment_time
    



def main():
    my_jira = MyJira("https://jira.amlogic.com", "lingzhi.bi", "Qwer!23456")
    project_id = my_jira.getProjectId("OTT-92553")
    print(project_id)
    SoftwareRelease = my_jira.getSoftwareRelease("OTT-92553")
    print(SoftwareRelease)
    # comments = my_jira.getComments("OTT-92107")
    # print(comments)
    # description = my_jira.getDescription("OTT-92107")
    # print(description)
    # summary = my_jira.getSummary("OTT-92107")
    # print(summary)


    # sql = "assignee = \"lingzhi.bi\" AND labels = LN_TAG_2025_AI"
    # sql = "project in (\"OTT projects\") AND status not in (Closed, Done, Resolved, Verified) AND priority in (High, Highest) AND type in (Bug, Sub-bug) OR labels = SE-LN-LOG-2026"
    # sql = "project in (\"OTT projects\") AND status not in (Closed, Done, Resolved, Verified) AND priority in (High, Highest) AND type in (Bug, Sub-bug) AND created >= \"2026-02-01\""
    sql = "key = OTT-92107"
    # priority_high_time = my_jira.getEarliestAttachmentTimeWithSql(sql)
    # print(f"len(priority_high_time):{len(priority_high_time)}")
    # print(priority_high_time)
    # label_applied_time = my_jira.getLabelAppliedTimeWithSql(sql, "SE-LN-LOG-2026")
    # print(f"len(label_applied_time):{len(label_applied_time)}")
    # print(label_applied_time)
if __name__ == "__main__":
    main()
