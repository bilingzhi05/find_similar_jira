from __future__ import annotations

import math
import re
from dataclasses import dataclass
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

@dataclass
class QAResult:
    question: str
    answer: str
    software_version: str
    hardware_version: str



def build_qa(
    summary: str,
    description_key_fields: dict[str, str],
    software_version: str,
    hardware_version: str,
    jira_id: str,
    comments_summary: str,
) -> QAResult:
    answer_parts = []
    if description_key_fields.get("repro_steps"):
        answer_parts.append(f"复现步骤：\n{description_key_fields['repro_steps']}")
    if description_key_fields.get("phenomenon"):
        answer_parts.append(f"现象：\n{description_key_fields['phenomenon']}")
    if description_key_fields.get("error_logs"):
        answer_parts.append(f"错误日志：\n{description_key_fields['error_logs']}")
    if software_version:
        answer_parts.append(f"软件版本：{software_version}")
    if hardware_version:
        answer_parts.append(f"硬件版本：{hardware_version}")
    answer_parts.append(f"jira_id：{jira_id}")
    if comments_summary:
        answer_parts.append(f"comments总结：\n{comments_summary}")
    answer = "\n\n".join(answer_parts).strip()
    return QAResult(
        question=summary,
        answer=answer,
        software_version=software_version,
        hardware_version=hardware_version,
    )


def _extract_root_cause_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    keywords = ["root cause", "原因", "导致", "根因"]
    return [line for line in lines if any(word.lower() in line.lower() for word in keywords)]


def extract_versions(text: str) -> tuple[str, str]:
    software = _extract_by_keywords(text, ["软件版本", "software version", "sw version"])
    hardware = _extract_by_keywords(text, ["硬件版本", "hardware", "hw version"])
    return software, hardware


def _extract_by_keywords(text: str, keywords: list[str]) -> str:
    if not text:
        return ""
    pattern = "|".join(re.escape(word) for word in keywords)
    match = re.search(rf"({pattern})[:：]?(.*)", text, re.IGNORECASE)
    if not match:
        return ""
    return match.group(2).strip().splitlines()[0]


def score_similarity(
    query_root_cause: str,
    query_error_logs: list[str],
    candidate_root_cause: str,
    candidate_error_logs: list[str],
    weight_root: float,
    weight_log: float,
) -> tuple[float, float, float]:
    sim_norm = _text_similarity(query_root_cause, candidate_root_cause)
    log_match = _log_match_count(query_error_logs, candidate_error_logs)
    cnt_norm = _normalize_count(log_match, max(1, len(query_error_logs)))
    score = weight_root * sim_norm + weight_log * cnt_norm
    return sim_norm, cnt_norm, score


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_./:-]+", text.lower())


def _text_similarity(a: str, b: str) -> float:
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    tf_a = _term_frequency(tokens_a)
    tf_b = _term_frequency(tokens_b)
    return _cosine_similarity(tf_a, tf_b)


def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    dot = sum(a.get(key, 0.0) * b.get(key, 0.0) for key in keys)
    norm_a = math.sqrt(sum(value * value for value in a.values())) or 1.0
    norm_b = math.sqrt(sum(value * value for value in b.values())) or 1.0
    return dot / (norm_a * norm_b)

import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from pydantic import BaseModel
import jieba
# 数据模型定义
class CompareRequest(BaseModel):
    user_causes: List[str]
    similar_causes: List[str]
# 停用词设置
STOP_WORDS = {'的', '在', '导致', '且', '未', '为', '了', '着', '是', '有', '对', '和', '与', '及', '或', '等', '之', '个', '这', '那', '都', '也', '就', '去', '又', '能', '会', '要', '将', '让', '但', '并', '给', '从', '向', '上', '下', '里', '外', '中', '前', '后', ' ', ',', '，', '.', '。', '、', ':', '：', ';', '；', '(', ')', '（', '）', '[', ']', '【', '】', '{', '}', '"', '"', "'", "'"}

def tokenize_with_jieba(text):
    if not text:
        return []
    words = jieba.lcut(text)
    return [word for word in words if word not in STOP_WORDS and word.strip()]

def compare_similarity(user_causes: list[str], similar_causes: list[str]):
    """
    使用 Sklearn 的 TF-IDF 余弦相似度算法进行比较 (Sklearn TF-IDF Cosine Similarity)
    """
    max_score = 0.0
    if not user_causes or not similar_causes:
        return [], max_score

    all_texts = user_causes + similar_causes
    
    try:
        # 每次请求动态构建 TF-IDF 矩阵
        # 注意：在生产环境中，如果有固定的语料库，应该预先训练好 vectorizer 并持久化
        # 这里为了演示方便，针对每次请求的文本集进行 fit_transform
        vectorizer = TfidfVectorizer(tokenizer=tokenize_with_jieba, token_pattern=None)
        tfidf_matrix = vectorizer.fit_transform(all_texts)
        
        user_vectors = tfidf_matrix[:len(user_causes)]
        similar_vectors = tfidf_matrix[len(user_causes):]
        
        similarity_matrix = cosine_similarity(user_vectors, similar_vectors)
        
        results = []
        
        for i, user_cause in enumerate(user_causes):
            for j, similar_cause in enumerate(similar_causes):
                score = float(similarity_matrix[i][j]) # Convert numpy float to python float
                if score > max_score:
                    max_score = score
                results.append({
                    "user_cause": user_cause,
                    "similar_cause": similar_cause,
                    "score": score
                })
        return results, max_score
    except Exception as e:
        raise RuntimeError(f"Error calculating similarity: {str(e)}") from e
if __name__ == "__main__":
    a_text = ["由于 buffer manager 初始化失败，内核触发空指针并崩溃"]
    b_text = ["buffer manager 初始化失败导致内核空指针崩溃"]
    print(compare_similarity(a_text, b_text))


   
   
   
