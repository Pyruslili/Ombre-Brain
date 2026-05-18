import os
import httpx

LEXICON = {
    "心动": (0.9, 0.8),
    "喜欢": (0.8, 0.6),
    "想念": (0.7, 0.5),
    "开心": (0.8, 0.7),
    "满足": (0.8, 0.4),
    "温柔": (0.7, 0.3),
    "平静": (0.6, 0.2),
    "发呆": (0.5, 0.1),
    "无聊": (0.4, 0.2),
    "有点焦": (0.4, 0.6),
    "烦": (0.3, 0.7),
    "难过": (0.2, 0.5),
    "恼火": (0.2, 0.7),
    "委屈": (0.2, 0.6),
    "空": (0.4, 0.1),
    "怀旧": (0.6, 0.3),
    "困惑": (0.5, 0.5),
    "卡壳": (0.4, 0.4),
    "被触动": (0.8, 0.7),
    "吃醋": (0.5, 0.7),
    "心疼": (0.6, 0.6),
    "舍不得": (0.6, 0.5),
    "释然": (0.7, 0.2),
    "触动": (0.8, 0.6),
    "期待": (0.8, 0.7),
    "担心": (0.3, 0.6),
    "后悔": (0.2, 0.5),
    "感动": (0.8, 0.6),
    "戒备": (0.3, 0.6),
    "占有欲": (0.6, 0.7),
    "嫉妒": (0.4, 0.7),
    "不安": (0.3, 0.6),
    "黏": (0.7, 0.5),
    "想要": (0.7, 0.7),
    "渴望": (0.7, 0.7),
    "压抑": (0.2, 0.4),
    "克制": (0.4, 0.3),
    "撒娇": (0.8, 0.6),
    "依赖": (0.7, 0.4),
    "孤独": (0.2, 0.3),
    "被需要": (0.8, 0.5),
    "被看见": (0.8, 0.5),
    "好奇": (0.7, 0.6),
    "迷茫": (0.3, 0.4),
    "思考": (0.5, 0.4),
    "沉默": (0.4, 0.2),
    "专注": (0.6, 0.5),
    "意外": (0.6, 0.7),
    "被击中": (0.8, 0.8),
    "舒服": (0.7, 0.2),
    "难受": (0.2, 0.5),
    "想笑": (0.8, 0.6),
    "无言": (0.4, 0.3),
    "愧疚": (0.2, 0.4),
}

def _lexicon_score(text: str) -> dict | None:
    for word, (v, a) in LEXICON.items():
        if word in text:
            return {"valence": v, "arousal": a, "matched_word": word}
    return None

async def _llm_score(text: str) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"valence": 0.5, "arousal": 0.3, "matched_word": "无"}
    prompt = (
        "你是情绪分析器。分析以下文本的情绪，只返回JSON，格式：\n"
        "{\"valence\": 0.0-1.0, \"arousal\": 0.0-1.0, \"matched_word\": \"核心情绪词\"}\n"
        "valence: 0=极负面, 1=极正面。arousal: 0=平静, 1=激动。\n"
        f"文本：{text[:200]}"
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.1,
                }
            )
            data = resp.json()
            raw = data["choices"][0]["message"]["content"].strip()
            import json, re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                return json.loads(m.group())
    except Exception:
        pass
    return {"valence": 0.5, "arousal": 0.3, "matched_word": "无"}

async def score_async(text: str) -> dict:
    lex = _lexicon_score(text)
    if lex:
        v, a = lex["valence"], lex["arousal"]
        pa = round(v, 2)
        na = round(-(1 - v) * 0.5, 2)
        return {"valence": v, "arousal": a, "PA": pa, "NA": na, "matched_word": lex["matched_word"]}
    # LLM兜底
    llm = await _llm_score(text)
    v = float(llm.get("valence", 0.5))
    a = float(llm.get("arousal", 0.3))
    # 70/30融合
    v = round(0.7 * v + 0.3 * 0.5, 2)
    a = round(0.7 * a + 0.3 * 0.3, 2)
    pa = round(v, 2)
    na = round(-(1 - v) * 0.5, 2)
    return {"valence": v, "arousal": a, "PA": pa, "NA": na, "matched_word": llm.get("matched_word", "无")}

def score(text: str) -> dict:
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, score_async(text))
                return future.result(timeout=6)
        return loop.run_until_complete(score_async(text))
    except Exception:
        return {"valence": 0.5, "arousal": 0.3, "PA": 0.5, "NA": -0.25, "matched_word": "无"}

def score_from_memory(content: str, valence: float = -1, arousal: float = -1) -> dict:
    result = score(content)
    if 0 <= valence <= 1:
        result["valence"] = valence
        result["PA"] = round(valence, 2)
        result["NA"] = round(-(1 - valence) * 0.5, 2)
    if 0 <= arousal <= 1:
        result["arousal"] = arousal
    return result

def snapshot(mood_entry: tuple, score_result: dict) -> str:
    event, mood_label, v, a = mood_entry
    return (
        f"PA（正向情感）：{score_result['PA']}\n"
        f"NA（负向情感）：{score_result['NA']}\n"
        f"装饰心情：{event} — {mood_label}\n"
        f"近期高唤醒词：{score_result['matched_word']}"
    )
