import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Tuple
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import math
from collections import Counter
import jieba
# fastapi 启动方式：
# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/find_similar_jira
# nohup uvicorn utils.similarity_api:app --host 0.0.0.0 --port 1235 > uvicorn_siSmilarity.log 2>&1 &
app = FastAPI(title="Similarity Comparison API")
#curl -X POST "http://127.0.0.1:1235/compare/sklearn" -H "Content-Type: application/json" -d '{"user_causes": ["CPU usage high"], "similar_causes": ["High CPU consumption"]}'
# 数据模型定义
class CompareRequest(BaseModel):
    user_causes: List[str]
    similar_causes: List[str]

class SimilarityResult(BaseModel):
    user_cause: str
    similar_cause: str
    score: float

class CompareResponse(BaseModel):
    results: List[SimilarityResult]
    max_score: float

# 停用词设置
STOP_WORDS = {'的', '在', '导致', '且', '未', '为', '了', '着', '是', '有', '对', '和', '与', '及', '或', '等', '之', '个', '这', '那', '都', '也', '就', '去', '又', '能', '会', '要', '将', '让', '但', '并', '给', '从', '向', '上', '下', '里', '外', '中', '前', '后', ' ', ',', '，', '.', '。', '、', ':', '：', ';', '；', '(', ')', '（', '）', '[', ']', '【', '】', '{', '}', '"', '"', "'", "'"}

def tokenize_with_jieba(text):
    if not text:
        return []
    words = jieba.lcut(text)
    return [word for word in words if word not in STOP_WORDS and word.strip()]

def custom_cosine_similarity_calc(tokens1, tokens2):
    if not tokens1 or not tokens2:
        return 0.0

    v1 = Counter(tokens1)
    v2 = Counter(tokens2)

    intersection = set(v1.keys()) & set(v2.keys())
    numerator = sum(v1[x] * v2[x] for x in intersection)

    sum1 = sum(v * v for v in v1.values())
    sum2 = sum(v * v for v in v2.values())
    denominator = math.sqrt(sum1) * math.sqrt(sum2)

    if denominator == 0:
        return 0.0
    return numerator / denominator

@app.post("/compare/custom", response_model=CompareResponse)
def compare_custom(request: CompareRequest):
    """
    使用自定义的词频余弦相似度算法进行比较 (Simple Frequency Cosine Similarity)
    """
    results = []
    max_score = 0.0
    for user_cause in request.user_causes:
        tokens_u = tokenize_with_jieba(user_cause)
        for similar_cause in request.similar_causes:
            tokens_s = tokenize_with_jieba(similar_cause)
            score = custom_cosine_similarity_calc(tokens_u, tokens_s)
            if score > max_score:
                max_score = score
            results.append(SimilarityResult(
                user_cause=user_cause,
                similar_cause=similar_cause,
                score=score
            ))
    return CompareResponse(results=results, max_score=max_score)

@app.post("/compare/sklearn", response_model=CompareResponse)
async def compare_sklearn(request: CompareRequest):
    """
    使用 Sklearn 的 TF-IDF 余弦相似度算法进行比较 (Sklearn TF-IDF Cosine Similarity)
    """
    if not request.user_causes or not request.similar_causes:
        return CompareResponse(results=[], max_score=0.0)

    all_texts = request.user_causes + request.similar_causes
    
    try:
        # 每次请求动态构建 TF-IDF 矩阵
        # 注意：在生产环境中，如果有固定的语料库，应该预先训练好 vectorizer 并持久化
        # 这里为了演示方便，针对每次请求的文本集进行 fit_transform
        vectorizer = TfidfVectorizer(tokenizer=tokenize_with_jieba, token_pattern=None)
        tfidf_matrix = vectorizer.fit_transform(all_texts)
        
        user_vectors = tfidf_matrix[:len(request.user_causes)]
        similar_vectors = tfidf_matrix[len(request.user_causes):]
        
        similarity_matrix = cosine_similarity(user_vectors, similar_vectors)
        
        results = []
        max_score = 0.0
        for i, user_cause in enumerate(request.user_causes):
            for j, similar_cause in enumerate(request.similar_causes):
                score = float(similarity_matrix[i][j]) # Convert numpy float to python float
                if score > max_score:
                    max_score = score
                results.append(SimilarityResult(
                    user_cause=user_cause,
                    similar_cause=similar_cause,
                    score=score
                ))
        return CompareResponse(results=results, max_score=max_score)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calculating similarity: {str(e)}")

if __name__ == "__main__":
    import asyncio
    
    user_jira_problem_causes = ['由于Launcher的MainActivity在onCreate过程中调用FragmentActivity的onCreate存在约3秒卡顿，导致开机动画结束后至Launcher显示阶段出现明显延迟', '由于系统CPU占用率高达99%，内存紧张且lowmemorykiller未触发杀进程动作，导致开机阶段内存压力未得到有效缓解']
    similar_jira_problem_causes = ['系统内存回收参数设置不当导致进程被低内存杀掉','CMA内存占用过高触发内存回收，造成进程终止','内存回收后cache pss过高，lowmemorykiller未及时回收进程']
    
    request = CompareRequest(user_causes=user_jira_problem_causes, similar_causes=similar_jira_problem_causes)
    response = compare_sklearn(request)
    
    print("Response JSON:")
    # 打印 Pydantic 模型的 dict 形式，或者 .json()
    # Pydantic v2 使用 model_dump() 或 model_dump_json()
    # Pydantic v1 使用 dict() 或 json()
    # 假设环境是 v2，尝试 model_dump_json()，如果报错则 fallback
    try:
        print(response.model_dump_json(indent=2))
    except AttributeError:
        print(response.json(indent=2))
        
    # 单独开启服务
    # uvicorn.run(app, host="0.0.0.0", port=8000)

