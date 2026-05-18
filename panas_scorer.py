from emotion_lexicon import LEXICON

def score(text: str) -> dict:
    best_word = None
    best_score = None
    for word, (v, a) in LEXICON.items():
        if word in text:
            best_word = word
            best_score = (v, a)
            break
    if best_score:
        v, a = best_score
    else:
        v, a = 0.5, 0.3
    pa = v
    na = -(1 - v) * 0.5
    return {
        "valence": v,
        "arousal": a,
        "PA": round(pa, 2),
        "NA": round(na, 2),
        "matched_word": best_word or "无",
    }

def snapshot(mood_entry: tuple, score_result: dict) -> str:
    event, mood_label, v, a = mood_entry
    return (
        f"PA（正向情感）：{score_result['PA']}\n"
        f"NA（负向情感）：{score_result['NA']}\n"
        f"装饰心情：{event} — {mood_label}\n"
        f"近期高唤醒词：{score_result['matched_word']}"
    )
def score_from_memory(content: str, valence: float = -1, arousal: float = -1) -> dict:
    """从记忆内容自动评分，用户传入的valence/arousal优先"""
    result = score(content)
    if 0 <= valence <= 1:
        result["valence"] = valence
        result["PA"] = round(valence, 2)
        result["NA"] = round(-(1 - valence) * 0.5, 2)
    if 0 <= arousal <= 1:
        result["arousal"] = arousal
    return result
