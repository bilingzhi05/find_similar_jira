import requests
from requests.auth import HTTPDigestAuth
import sys
import json
import fastapi
# fastapi 启动方式：
# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira/utils
# nohup uvicorn gerrit_info:app --host 0.0.0.0 --port 1236 > uvicorn_gerrit_info.log 2>&1 &
app = fastapi.FastAPI()

class GerritClient:
    def __init__(self, base_url, username, password, use_digest=False, headers=None):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.use_digest = use_digest
        self.headers = headers

    def _auth(self):
        if self.use_digest:
            return HTTPDigestAuth(self.username, self.password)
        return (self.username, self.password)

    def _request(self, path, params=None):
        return requests.get(
            f"{self.base_url}{path}",
            params=params,
            auth=self._auth(),
            timeout=10,
            headers=self.headers,
        )

    def _handle_response(self, resp):
        if resp.status_code != 200:
            print(f"Request failed with status code: {resp.status_code}")
            print(f"Response: {resp.text}")
            error_response = {
                "status_code": resp.status_code,
                "error": resp.text.rstrip("\n")
            }
            return json.dumps(error_response)
        return resp.text.lstrip(")]}'\n")

    def get_change_info(self, change_id, params=None):
        base_params = {
            "q": f"change:{change_id}",
        }
        if params:
            base_params.update(params)
        resp = self._request("/a/changes/", params=base_params)
        return self._handle_response(resp)

    def get_change_detail(self, change_id, params=None):
        base_params = {
            "o": ["CURRENT_REVISION", "CURRENT_COMMIT"]
        }
        if params:
            base_params.update(params)
        resp = self._request(f"/a/changes/{change_id}/detail", params=base_params)
        return self._handle_response(resp)


class GerritService:
    def __init__(self, user: str = "lingzhi.bi", 
    source_repo_pw: str = "We/jHb0eSZT+yTxGnhs722PF7EJy+81O1x8cK+tXnQ", 
    aml_code_master_pw: str = "Qwer!23456", 
    scgit_pw: str = "IeAO/9jzeYjsZVOrBr8AI6qqRO4K3mNNqXPI8OerhQ"):
        self.source_client = GerritClient(
            "https://source.amlogic.com",
            user,
            source_repo_pw,
        )
        self.aml_code_master_client = GerritClient(
            "https://aml-code-master.amlogic.com",
            user,
            aml_code_master_pw,
        )
        self.scgit_client = GerritClient(
            "https://scgit.amlogic.com",
            user,
            scgit_pw,
            use_digest=True,
            headers={"Accept": "application/json"},
        )

    def source_get_change_info(self, change_id: str):
        return self.source_client.get_change_info(change_id)

    def aml_code_master_get_change_info(self, change_id: str):
        return self.aml_code_master_client.get_change_info(change_id)

    def scgit_get_change_info(self, change_id: str):
        try:
            return self.scgit_client.get_change_info(
                change_id,
                params={"o": ["CURRENT_REVISION", "CURRENT_COMMIT", "DETAILED_ACCOUNTS"]},
            )
        except Exception as e:
            print(f"An error occurred: {str(e)}")

    def aml_code_master_get_change_detail(self, change_id: str):
        return self.aml_code_master_client.get_change_detail(change_id)

    def source_get_change_detail(self, change_id: str):
        return self.source_client.get_change_detail(change_id)

    def scgit_get_change_detail(self, change_id: str):
        try:
            return self.scgit_client.get_change_detail(change_id)
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            return str(e)


gerrit_service = GerritService()

@app.get("/source_get_change_info/{change_id}")
def source_get_change_info(change_id: str):
    return gerrit_service.source_get_change_info(change_id)

@app.get("/aml_code_master_get_change_info/{change_id}")
def aml_code_master_get_change_info(change_id: str):
    return gerrit_service.aml_code_master_get_change_info(change_id)

@app.get("/scgit_get_change_info/{change_id}")
def scgit_get_change_info(change_id: str):
    return gerrit_service.scgit_get_change_info(change_id)



@app.get("/aml_code_master_get_change_detail/{change_id}")
def aml_code_master_get_change_detail(change_id: str):
    return gerrit_service.aml_code_master_get_change_detail(change_id)

@app.get("/source_get_change_detail/{change_id}")
def source_get_change_detail(change_id: str):
    return gerrit_service.source_get_change_detail(change_id)

@app.get("/scgit_get_change_detail/{change_id}")
def scgit_get_change_detail(change_id: str):
    return gerrit_service.scgit_get_change_detail(change_id)


if __name__ == "__main__":
    ret = gerrit_service.scgit_get_change_detail("570691")
    print(f"scgit_get_change_detail:{ret}", type(ret))
    ret = gerrit_service.aml_code_master_get_change_detail("43992")
    print(f"aml_code_master_get_change_detail:{ret}", type(ret))
    ret = gerrit_service.source_get_change_detail("47154")
    print(f"source_get_change_detail:{ret}", type(ret))
    ret = gerrit_service.scgit_get_change_info("I380044dae3b4dcea9c72bf6474e6c73e09ba6d97")
    print(f"scgit_get_change_info:{ret}", type(ret))
    ret = gerrit_service.aml_code_master_get_change_info("Ide563b9900f10c5585ac8002645795d91b802cc5")
    print(f"aml_code_master_get_change_info:{ret}", type(ret))
    ret = gerrit_service.source_get_change_info("I6ffe40512440a2c08b0797d32285c4e3beca1f00")
    print(f"source_get_change_info:{ret}", type(ret))
    ret = gerrit_service.aml_code_master_get_change_info("43992")
    print(f"aml_code_master_get_change_info:{ret}", type(ret))
