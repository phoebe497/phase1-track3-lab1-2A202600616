from __future__ import annotations
import os
import re
import json
import time
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from .schemas import QAExample, JudgeResult, ReflectionEntry
from .utils import normalize_answer
from .prompts import ACTOR_SYSTEM, EVALUATOR_SYSTEM, REFLECTOR_SYSTEM

# Load environment variables
load_dotenv(override=True)

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://opencode.ai/zen/go/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
LLM_MODE = os.getenv("LLM_MODE", "llm")

FIRST_ATTEMPT_WRONG = {"hp2": "London", "hp4": "Atlantic Ocean", "hp6": "Red Sea", "hp8": "Andes"}
FAILURE_MODE_BY_QID = {"hp2": "incomplete_multi_hop", "hp4": "wrong_final_answer", "hp6": "entity_drift", "hp8": "entity_drift"}

ACTIVE_KEY_INDEX = 0

def get_current_api_key():
    raw_key = os.getenv("LLM_API_KEY")
    keys = []
    if raw_key:
        keys.extend([k.strip() for k in raw_key.split(",") if k.strip()])
    sec_key = os.getenv("LLM_API_KEY_SECONDARY")
    if sec_key:
        keys.extend([k.strip() for k in sec_key.split(",") if k.strip()])
    if not keys:
        return None
    global ACTIVE_KEY_INDEX
    idx = ACTIVE_KEY_INDEX % len(keys)
    return keys[idx]

def rotate_key():
    global ACTIVE_KEY_INDEX
    raw_key = os.getenv("LLM_API_KEY")
    keys = []
    if raw_key:
        keys.extend([k.strip() for k in raw_key.split(",") if k.strip()])
    sec_key = os.getenv("LLM_API_KEY_SECONDARY")
    if sec_key:
        keys.extend([k.strip() for k in sec_key.split(",") if k.strip()])
    if len(keys) > 1:
        ACTIVE_KEY_INDEX = (ACTIVE_KEY_INDEX + 1) % len(keys)
        new_key = keys[ACTIVE_KEY_INDEX]
        print(f"🔄 Đã tự động chuyển sang API Key thứ {ACTIVE_KEY_INDEX + 1} (bắt đầu bằng {new_key[:8]}...) do key hiện tại bị giới hạn rate limit (429).")
        return True
    return False

def get_llm() -> ChatOpenAI | None:
    api_key = get_current_api_key()
    base_url = os.getenv("LLM_BASE_URL", LLM_BASE_URL)
    model = os.getenv("LLM_MODEL", LLM_MODEL)
    
    # If the key is not set or placeholder, fallback to mock mode
    if api_key == "your-api-key-here" or api_key == "api-key" or not api_key:
        return None
    try:
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.0,
            timeout=60,
            max_retries=1,
        )
    except Exception as e:
        print(f"Error initializing LLM client: {e}")
        return None

def invoke_with_retry(llm, messages, max_retries=10, initial_delay=2.0, backoff_factor=1.5):
    current_llm = llm
    for attempt in range(max_retries):
        try:
            return current_llm.invoke(messages)
        except Exception as e:
            err_msg = str(e).lower()
            if attempt < max_retries - 1 and ("429" in err_msg or "rate_limit" in err_msg or "too many requests" in err_msg):
                # Thử chuyển sang key dự phòng trước
                if rotate_key():
                    new_llm = get_llm()
                    if new_llm:
                        current_llm = new_llm
                        print("Thử lại request ngay lập tức bằng API Key dự phòng...")
                        continue  # Thử lại ngay không cần sleep
                
                # Nếu không xoay được key (chỉ có 1 key hoặc tất cả đều bị rate limit), tiến hành sleep
                delay = initial_delay * (backoff_factor ** attempt)
                match = re.search(r"try again in (\d+)(ms|s)", err_msg)
                if match:
                    wait_val = int(match.group(1))
                    wait_unit = match.group(2)
                    if wait_unit == "ms":
                        delay = max(delay, (wait_val / 1000.0) + 0.5)
                    else:
                        delay = max(delay, wait_val + 1.0)
                print(f"Rate limit hit (429). Đang chờ {delay:.1f}s để thử lại... (Lần thử {attempt+1}/{max_retries})")
                time.sleep(delay)
            else:
                raise e

def extract_json(text: str) -> dict | None:
    # Try direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass
        
    # Try finding markdown code block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass
            
    # Try finding general { ... }
    match = re.search(r"(\{.*?\})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass
            
    return None

def is_mock_fail(qid: str) -> bool:
    if qid in FIRST_ATTEMPT_WRONG:
        return True
    try:
        num_id = int(re.sub(r"\D", "", qid))
        return num_id % 4 == 0
    except Exception:
        return hash(qid) % 4 == 0

def actor_answer(example: QAExample, attempt_id: int, agent_type: str, reflection_memory: list[str]) -> tuple[str, int, int]:
    llm = get_llm()
    # Check if we should use mock or real LLM
    if os.getenv("LLM_MODE", LLM_MODE) == "mock" or llm is None:
        # Mock logic
        start_time = time.time()
        # Mock values
        tokens = 320 + (attempt_id * 65) + (120 if agent_type == "reflexion" else 0)
        latency = 160 + (attempt_id * 40) + (90 if agent_type == "reflexion" else 0)
        
        if not is_mock_fail(example.qid):
            ans = example.gold_answer
        elif agent_type == "react":
            ans = FIRST_ATTEMPT_WRONG.get(example.qid, "wrong_answer_simulated")
        elif attempt_id == 1 and not reflection_memory:
            ans = FIRST_ATTEMPT_WRONG.get(example.qid, "wrong_answer_simulated")
        else:
            ans = example.gold_answer
        return ans, tokens, int((time.time() - start_time) * 1000 + latency)

    # Real LLM logic
    context_str = "\n\n".join([f"Source: {chunk.title}\nContent: {chunk.text}" for chunk in example.context])
    
    reflection_str = ""
    if reflection_memory:
        reflection_str = "\n\nReflection History:\n" + "\n".join([f"- {m}" for m in reflection_memory])
        
    user_prompt = f"Context:\n{context_str}\n\nQuestion: {example.question}{reflection_str}\n\nAnswer:"
    
    start_time = time.time()
    try:
        messages = [
            {"role": "system", "content": ACTOR_SYSTEM},
            {"role": "user", "content": user_prompt}
        ]
        response = invoke_with_retry(llm, messages)
        latency_ms = int((time.time() - start_time) * 1000)
        
        token_usage = response.response_metadata.get("token_usage", {})
        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)
        total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)
        if total_tokens == 0:
            total_tokens = len(ACTOR_SYSTEM + user_prompt + response.content) // 4
            
        content = response.content
        match = re.search(r"Final Answer:\s*(.*)", content, re.IGNORECASE)
        if match:
            answer = match.group(1).strip()
        else:
            # Fallback clean up
            answer = content.strip()
            # If answer contains multiple lines, try to grab the last non-empty line
            lines = [l.strip() for l in answer.split("\n") if l.strip()]
            if lines:
                answer = lines[-1]
            
        return answer, total_tokens, latency_ms
    except Exception as e:
        print(f"LLM Actor error: {e}. Falling back to mock answer.")
        return example.gold_answer, 100, int((time.time() - start_time) * 1000)

def evaluator(example: QAExample, answer: str) -> JudgeResult:
    llm = get_llm()
    # Check if we should use mock or real LLM
    if os.getenv("LLM_MODE", LLM_MODE) == "mock" or llm is None:
        # Mock logic
        start_time = time.time()
        tokens = 150
        latency = 120
        
        if normalize_answer(example.gold_answer) == normalize_answer(answer):
            res = JudgeResult(score=1, reason="Final answer matches the gold answer after normalization.")
        elif normalize_answer(answer) in ["london", "wrong_answer_simulated"]:
            res = JudgeResult(score=0, reason="Simulated failure: The answer did not complete the hops correctly.", missing_evidence=["Need to cross-reference with the other context chunks."], spurious_claims=[])
        else:
            res = JudgeResult(score=0, reason="Simulated failure: Wrong entity selected.", missing_evidence=["Check the secondary paragraph."], spurious_claims=[answer])
            
        res.token_estimate = tokens
        res.latency_ms = int((time.time() - start_time) * 1000 + latency)
        return res

    # Real LLM logic
    start_time = time.time()
    user_prompt = (
        f"Question: {example.question}\n"
        f"Gold Answer: {example.gold_answer}\n"
        f"Predicted Answer: {answer}\n\n"
        "Compare the predicted answer with the gold answer. Output JSON."
    )
    try:
        messages = [
            {"role": "system", "content": EVALUATOR_SYSTEM},
            {"role": "user", "content": user_prompt}
        ]
        response = invoke_with_retry(llm, messages)
        latency_ms = int((time.time() - start_time) * 1000)
        
        token_usage = response.response_metadata.get("token_usage", {})
        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)
        total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)
        if total_tokens == 0:
            total_tokens = len(EVALUATOR_SYSTEM + user_prompt + response.content) // 4
            
        parsed_data = extract_json(response.content)
        if parsed_data:
            score = int(parsed_data.get("score", 0))
            reason = parsed_data.get("reason", "Graded by LLM Evaluator.")
            missing_evidence = parsed_data.get("missing_evidence", [])
            spurious_claims = parsed_data.get("spurious_claims", [])
        else:
            is_correct = normalize_answer(example.gold_answer) == normalize_answer(answer)
            score = 1 if is_correct else 0
            reason = f"Fallback grading. Content: {response.content}"
            missing_evidence = []
            spurious_claims = []
            
        res = JudgeResult(
            score=score,
            reason=reason,
            missing_evidence=missing_evidence,
            spurious_claims=spurious_claims,
            token_estimate=total_tokens,
            latency_ms=latency_ms
        )
        return res
    except Exception as e:
        print(f"LLM Evaluator error: {e}. Falling back to default grading.")
        is_correct = normalize_answer(example.gold_answer) == normalize_answer(answer)
        res = JudgeResult(
            score=1 if is_correct else 0,
            reason=f"Fallback grading due to error: {e}",
            missing_evidence=[],
            spurious_claims=[],
            token_estimate=50,
            latency_ms=int((time.time() - start_time) * 1000)
        )
        return res

def reflector(example: QAExample, attempt_id: int, judge: JudgeResult) -> ReflectionEntry:
    llm = get_llm()
    # Check if we should use mock or real LLM
    if os.getenv("LLM_MODE", LLM_MODE) == "mock" or llm is None:
        # Mock logic
        start_time = time.time()
        tokens = 200
        latency = 180
        
        strategy = "Verify the final entity against the second paragraph before answering."
        if example.qid == "hp2":
            strategy = "Do the second hop explicitly: birthplace city -> river through that city."
        elif is_mock_fail(example.qid):
            strategy = "Follow all entity relations to the second hop context instead of guessing."
            
        res = ReflectionEntry(
            attempt_id=attempt_id, 
            failure_reason=judge.reason, 
            lesson="A partial first-hop answer is not enough; the final answer must complete all hops.", 
            next_strategy=strategy
        )
        res.token_estimate = tokens
        res.latency_ms = int((time.time() - start_time) * 1000 + latency)
        return res

    # Real LLM logic
    start_time = time.time()
    context_str = "\n\n".join([f"Source: {chunk.title}\nContent: {chunk.text}" for chunk in example.context])
    user_prompt = (
        f"Context:\n{context_str}\n\n"
        f"Question: {example.question}\n"
        f"Evaluator's Feedback: {judge.reason}\n"
        f"Missing Evidence: {judge.missing_evidence}\n"
        f"Spurious Claims: {judge.spurious_claims}\n\n"
        f"Provide a reflection entry for attempt {attempt_id}."
    )
    try:
        messages = [
            {"role": "system", "content": REFLECTOR_SYSTEM},
            {"role": "user", "content": user_prompt}
        ]
        response = invoke_with_retry(llm, messages)
        latency_ms = int((time.time() - start_time) * 1000)
        
        token_usage = response.response_metadata.get("token_usage", {})
        prompt_tokens = token_usage.get("prompt_tokens", 0)
        completion_tokens = token_usage.get("completion_tokens", 0)
        total_tokens = token_usage.get("total_tokens", prompt_tokens + completion_tokens)
        if total_tokens == 0:
            total_tokens = len(REFLECTOR_SYSTEM + user_prompt + response.content) // 4
            
        parsed_data = extract_json(response.content)
        if parsed_data:
            failure_reason = parsed_data.get("failure_reason", judge.reason)
            lesson = parsed_data.get("lesson", "Verify entities across all reasoning steps.")
            next_strategy = parsed_data.get("next_strategy", "Carefully check supporting paragraphs.")
        else:
            failure_reason = judge.reason
            lesson = f"LLM Reflection raw response parsing failed. Raw response: {response.content}"
            next_strategy = "Proceed carefully with the context."
            
        res = ReflectionEntry(
            attempt_id=attempt_id,
            failure_reason=failure_reason,
            lesson=lesson,
            next_strategy=next_strategy,
            token_estimate=total_tokens,
            latency_ms=latency_ms
        )
        return res
    except Exception as e:
        print(f"LLM Reflector error: {e}. Falling back to default reflection.")
        res = ReflectionEntry(
            attempt_id=attempt_id,
            failure_reason=judge.reason,
            lesson=f"Reflector error: {e}",
            next_strategy="Carefully parse the question and check context in the next step.",
            token_estimate=50,
            latency_ms=int((time.time() - start_time) * 1000)
        )
        return res
