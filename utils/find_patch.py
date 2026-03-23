import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import fastapi
from utils.jira_client import MyJira
from utils.logger import log as mylog
from utils.gerrit_info import gerrit_service

app = fastapi.FastAPI()
# fastapi 启动方式：
# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira
# nohup uvicorn utils.find_patch:app --host 0.0.0.0 --port 1236 > uvicorn_find_patch.log 2>&1 &
def extract_patches_from_comments(comments: list[str]) -> list[dict]:
    url_pattern = re.compile(r"https?:\/\/[^\s\]\)]+")
    change_id_pattern = re.compile(r"\/(\d+)(?:/)?$")

    all_urls = []
    seen = set()
    result = []

    for comment in comments:
        if not isinstance(comment, str):
            continue
        # 只处理包含 Change proposed 的 comment
        if "Change proposed" not in comment:
            continue

        urls = url_pattern.findall(comment)
        for url in urls:
            match = change_id_pattern.search(url)
            if not match:
                continue
            if url not in seen:
                all_urls.append(url)
                seen.add(url)
                result.append(
                    {
                        "url": url,
                        "change_id": match.group(1),
                    }
                )

    return result, all_urls

def fetch_change_info(patch: dict, max_retries: int = 3, backoff_seconds: float = 1.0) -> str | None:
    url = patch.get("url") or ""
    change_id = patch.get("change_id")
    if not change_id:
        return None

    if "https://scgit.amlogic.com" in url:
        getter = gerrit_service.scgit_get_change_info
    elif "https://source.amlogic.com" in url:
        getter = gerrit_service.source_get_change_info
    elif "https://aml-code-master.amlogic.com" in url:
        getter = gerrit_service.aml_code_master_get_change_info
    else:
        return None

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return getter(change_id)
        except Exception as exc:
            last_error = exc
            mylog(f"request failed: error={exc} attempt={attempt}")
        if attempt < max_retries:
            time.sleep(backoff_seconds * attempt)
    if last_error:
        mylog(f"request failed permanently: error={last_error}")
    return None

def fetch_change_detail(patch: dict, max_retries: int = 3, backoff_seconds: float = 1.0) -> str | None:
    url = patch.get("url") or ""
    change_id = patch.get("change_id")
    if not change_id:
        return None

    if "https://scgit.amlogic.com" in url:
        getter = gerrit_service.scgit_get_change_detail
    elif "https://source.amlogic.com" in url:
        getter = gerrit_service.source_get_change_detail
    elif "https://aml-code-master.amlogic.com" in url:
        getter = gerrit_service.aml_code_master_get_change_detail
    else:
        return None

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return getter(change_id)
        except Exception as exc:
            last_error = exc
            mylog(f"request failed: error={exc} attempt={attempt}")
        if attempt < max_retries:
            time.sleep(backoff_seconds * attempt)
    if last_error:
        mylog(f"request failed permanently: error={last_error}")
    return None


def _parse_change_detail(detail_text: str | None) -> dict | None:
    if not detail_text:
        return None
    try:
        data = json.loads(detail_text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return data
    return None


def find_merged_change_in_other_repos(change_id: str) -> dict:
    sources = [
        "https://source.amlogic.com",
        "https://aml-code-master.amlogic.com",
    ]
    project_branches = []
    for base_url in sources:
        detail_text = fetch_change_info({"url": base_url, "change_id": change_id})
        details = _parse_change_detail(detail_text)
        if not details:
            continue
        # mylog(f"details: {details}")
        for detail in details:
            if detail.get("status") == "MERGED":
                project = detail.get("project")
                branch = detail.get("branch")
                if project and branch:
                    project_branches.append({"project": project, "branch": branch})
    return project_branches


def _load_repo_routes(repo_route_path: str) -> list[dict]:
    path = Path(repo_route_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _get_repo_path_by_project_id(project_id: str, repo_route_path: str) -> str | None:
    routes = _load_repo_routes(repo_route_path)
    for item in routes:
        if item.get("Project ID") == project_id:
            return item.get("repo_path")
    return None


def _collect_manifest_projects(manifest_path: str, visited=None) -> list[tuple[str, str]]:
    path = Path(manifest_path)
    if not path.exists():
        return []
    if visited is None:
        visited = set()
    if str(path) in visited:
        return []
    visited.add(str(path))
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    root = tree.getroot()
    projects = []
    for node in root.findall("project"):
        name = node.attrib.get("name") or ""
        revision = node.attrib.get("revision") or ""
        projects.append((name, revision))
    for node in root.findall("include"):
        include_name = node.attrib.get("name")
        if not include_name:
            continue
        include_path = path.parent / include_name
        projects.extend(_collect_manifest_projects(str(include_path), visited))
    return projects


def _manifest_contains_project_branch(manifest_path: str, project: str, branch: str) -> bool:
    if not project or not branch:
        return False
    for name, revision in _collect_manifest_projects(manifest_path):
        if project in name and branch in revision:
            mylog(f"project:{project}; name:{name} ")
            mylog(f"branch:{branch}; revision:{revision}")
            return True
    return False


def find_url_if_project_in_manifest(
    my_jira: MyJira,
    issue_key: str,
    similar_jira_info: list[dict],
    repo_route_path: str = "/home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira/A_REPO_PATH/repo_route.json",
) -> bool:
    project_id = my_jira.getProjectId(issue_key)
    if not project_id:
        return False
    # 判断当前的查询的jira项目是否在repo_route.json中记录
    repo_path = _get_repo_path_by_project_id(project_id, repo_route_path)
    if not repo_path:
        return False
    for item in similar_jira_info:
        project = item.get("project")
        if isinstance(project, str):
            parts = [p for p in project.split("/") if p]
            if len(parts) >= 2:
                project = "/".join(parts[-2:])
        branch = item.get("branch")
        if not project or not branch:
            continue
        if _manifest_contains_project_branch(repo_path, project, branch):
            return True
    return False


def collect_patch_urls(similar_jira_id: str, user_jira_id: str, my_jira: MyJira) -> tuple[list[str], list[str]]:
    comments = my_jira.getComments(similar_jira_id)
    patches, all_urls = extract_patches_from_comments(comments)
    merge_urls = []
    for patch in patches:
        change_id = patch.get("change_id")
        if not change_id:
            continue
        patch_url = patch.get("url")
        if not patch_url:
            continue
        patch_detail_text = fetch_change_detail(patch)
        # mylog(f"patch_detail_text: {patch_detail_text}")
        patch_info = _parse_change_detail(patch_detail_text)
        if patch_info:
            change_id = patch_info.get("change_id") #获取提交信息中的changeid
        mylog(f"change_id: {change_id}")
        merged_info = find_merged_change_in_other_repos(change_id)
        mylog(f"merged_info: {merged_info}")
        if find_url_if_project_in_manifest(my_jira, user_jira_id, merged_info):
            url = patch.get("url")
            if url:
                merge_urls.append(url)
    return all_urls, merge_urls


@app.get("/collect_patch_urls")
def collect_patch_urls_api(similar_jira_id: str, user_jira_id: str):
    my_jira = MyJira("https://jira.amlogic.com", "lingzhi.bi", "Qwer!23456")
    all_urls, merge_urls = collect_patch_urls(similar_jira_id, user_jira_id, my_jira)
    mylog(f"all_urls: {all_urls}")
    mylog(f"merge_urls: {merge_urls}")
    return {
        "all_urls": all_urls,
        "merge_urls": merge_urls,
    }

if __name__ == "__main__":
    # comments = [
    #     "Change proposed: https://gerrit.amlogic.com/c/12345",
    #     "Another change: https://gerrit.amlogic.com/c/67890",
    # ]
    my_jira = MyJira("https://jira.amlogic.com", "lingzhi.bi", "Qwer!23456")
    similar_jira_id = "OTT-75987"
    user_jira_id = "OTT-92490" # 有没有合入这个项目的分支
    all_urls, merge_urls = collect_patch_urls(similar_jira_id, user_jira_id, my_jira)
    mylog(f"all_urls: {all_urls}")
    mylog(f"merge_urls: {merge_urls}")
