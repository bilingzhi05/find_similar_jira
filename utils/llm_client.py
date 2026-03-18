from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class LLMConfig:
    provider: str = "ollama"
    model: str = "qwen3:4b-instruct-2507-fp16"
    temperature: float = 0.1
    top_p: float = 0.3
    max_tokens: int = 512
    max_output_chars: int = 600
    api_base: str | None = "http://10.58.11.60:11434"
    api_key_env: str = "OPENAI_API_KEY"
    context_length: int = 4096


class LLMClient:
    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self._llm = None

    def qa(self, prompt_text: str) -> str:
        llm = self._get_llm()
        result = llm.invoke(prompt_text)
        if hasattr(result, "content"):
            return (result.content or "").strip()
        return str(result).strip()

    def qa_with_system(self, system_prompt: str, user_prompt: str) -> str:
        llm = self._get_llm()
        messages = self._build_messages(system_prompt, user_prompt)
        result = llm.invoke(messages)
        if hasattr(result, "content"):
            return (result.content or "").strip()
        return str(result).strip()

    def qa_with_system_structured(self, system_prompt: str, user_prompt: str) -> dict:
        format_prompt = (
            f"{system_prompt}\n\n"
            '输出必须是严格JSON，格式为: {"output": "..."}'
        )
        text = self.qa_with_system(format_prompt, user_prompt)
        return self._parse_output(text)

    def _build_prompt(self, question: str, context: str) -> str:
        try:
            from langchain_core.prompts import PromptTemplate
        except ImportError as exc:
            raise RuntimeError("langchain 未安装或版本不支持 PromptTemplate") from exc
        template = "你是Jira问题分析助手。\n\n上下文：\n{context}\n\n问题：\n{question}\n\n回答："
        prompt = PromptTemplate(template=template, input_variables=["context", "question"])
        return prompt.format(context=context, question=question)

    def _build_messages(self, system_prompt: str, user_prompt: str):
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError as exc:
            raise RuntimeError("langchain 未安装或版本不支持 messages") from exc
        return [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

    def _parse_output(self, text: str) -> dict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"output": text}
        if isinstance(data, dict) and "output" in data:
            return data
        return {"output": text}

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        if self.config.provider == "ollama":
            self._llm = self._build_ollama_llm()
            return self._llm
        if self.config.provider in {"openai", "openai_chat"}:
            api_key = os.getenv(self.config.api_key_env)
            if not api_key:
                raise RuntimeError(f"未找到环境变量 {self.config.api_key_env}")
            self._llm = self._build_openai_llm(api_key)
            return self._llm
        raise RuntimeError(f"不支持的 provider: {self.config.provider}")

    def _build_openai_llm(self, api_key: str):
        try:
            from langchain.chat_models import ChatOpenAI
        except ImportError:
            ChatOpenAI = None
        if ChatOpenAI is not None:
            return ChatOpenAI(
                model=self.config.model,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                openai_api_key=api_key,
                openai_api_base=self.config.api_base,
            )
        try:
            from langchain.llms import OpenAI
        except ImportError as exc:
            raise RuntimeError("langchain 未包含 OpenAI LLM 实现") from exc
        return OpenAI(
            model_name=self.config.model,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            openai_api_key=api_key,
            openai_api_base=self.config.api_base,
        )

    def _build_ollama_llm(self):
        try:
            from langchain_ollama import Ollama
        except ImportError:
            Ollama = None
        if Ollama is None:
            try:
                from langchain_ollama import OllamaLLM as Ollama
            except ImportError as exc:
                raise RuntimeError("langchain 未包含 Ollama LLM 实现") from exc
        return Ollama(
            model=self.config.model,
            base_url=self.config.api_base,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            num_ctx=self.config.context_length,
        )


LLM_PRESETS = {
    "ollama_qwen3_4b": {
        "provider": "ollama",
        "model": "qwen3:4b-instruct-2507-fp16",
        "temperature": 0.1,
        "top_p": 0.3,
        "max_tokens": 512,
        "max_output_chars": 600,
        "api_base": "http://10.58.11.60:11434",
        "api_key_env": "OPENAI_API_KEY",
        "context_length": 4096,
    },
    "ollama_qwen3_8b-q8": {
        "provider": "ollama",
        "model": "qwen3:8b-q8_0",
        "temperature": 0.1,
        "top_p": 0.3,
        "max_tokens": 512,
        "max_output_chars": 600,
        "api_base": "http://10.58.11.60:11434",
        "api_key_env": "OPENAI_API_KEY",
        "context_length": 4096,
    },
    "openai_default": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "temperature": 0.1,
        "top_p": 0.3,
        "max_tokens": 512,
        "max_output_chars": 600,
        "api_base": None,
        "api_key_env": "OPENAI_API_KEY",
        "context_length": 4096,
    },
}


def build_llm_client(config: dict | None = None, preset_name: str | None = None) -> LLMClient:
    if not config:
        config = {}
    preset_name = preset_name or config.get("preset", "ollama_qwen3_4b")
    preset = LLM_PRESETS.get(preset_name, LLM_PRESETS["ollama_qwen3_4b"])
    merged = {**preset, **config}
    llm_config = LLMConfig(
        provider=merged.get("provider", "ollama"),
        model=merged.get("model", "qwen3:4b-instruct-2507-fp16"),
        temperature=float(merged.get("temperature", 0.1)),
        top_p=float(merged.get("top_p", 0.3)),
        max_tokens=int(merged.get("max_tokens", 512)),
        max_output_chars=int(merged.get("max_output_chars", 600)),
        api_base=merged.get("api_base", "http://10.58.11.60:11434"),
        api_key_env=merged.get("api_key_env", "OPENAI_API_KEY"),
        context_length=int(merged.get("context_length", 4096)),
    )
    return LLMClient(llm_config)

if __name__ == "__main__":
    llm_client = build_llm_client(preset_name="ollama_qwen3_8b-q8")
    # prompt_text = llm_client._build_prompt("你是谁？", "")
    user_prompt = """
    问题：你是谁？
    """
    system_prompt = "你是一个问答助手，根据用户的问题，回答用户的问题。"
    print(llm_client.qa_with_system_structured(system_prompt=system_prompt, user_prompt=user_prompt))
