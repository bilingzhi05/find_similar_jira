from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.pipeline import run_pipeline


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")) or {}


class RunRequest(BaseModel):
    key: str
    limit_score: float = 4.0
    config_path: str | None = "config.json"


class RunResponse(BaseModel):
    status: str
    report_path: str | None = None
    content: str | None = None
    message: str | None = None


app = FastAPI()


@app.post("/run")
def run(request: RunRequest):
    config_path = request.config_path or "config.json"
    config = load_config(config_path)
    try:
        result = run_pipeline(config.get("pipeline", {}), request.key, request.limit_score)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result == "没有相似的jira":
        return "没有相似的jira"
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira && nohup /home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira/310venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8801 &
# curl -X POST http://10.18.11.98:8801/run   -H "Content-Type: application/json"   -d '{"key":"OTT-80575", "limit_score": 0.1}'
# curl -X POST http://10.18.11.98:5678/webhook/6ab10dbf-637a-4239-8b0e-bf58ba00c6fe  -H "Content-Type: application/json"   -d '{"user_key":"OTT-80575", "limit_score": 0.1}'