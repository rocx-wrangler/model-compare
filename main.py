"""Model Compare PoC: one prompt -> several DigitalOcean serverless-inference models."""
import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = os.environ.get("INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
KEY = os.environ.get("MODEL_ACCESS_KEY", "")
MAX_MODELS = int(os.environ.get("MAX_MODELS", "4"))                # models per comparison
TOKEN_CAP = int(os.environ.get("TOKEN_CAP", "4000"))               # hard cap on output tokens
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "10"))       # comparisons per client per minute
GLOBAL_RATE_LIMIT = int(os.environ.get("GLOBAL_RATE_LIMIT_PER_MIN", "30"))  # all clients combined
DAILY_LIMIT = int(os.environ.get("DAILY_COMPARE_LIMIT", "500"))             # comparisons per UTC day, 0 = off
# Header that carries the real client IP, set ONLY if your platform's proxy sets it.
# On DigitalOcean App Platform use: do-connecting-ip. Empty = use the direct socket peer.
CLIENT_IP_HEADER = os.environ.get("CLIENT_IP_HEADER", "").strip().lower()

NON_CHAT = ("embed", "mini-lm", "bge", "gte", "whisper", "tts", "image", "flux",
            "sdxl", "diffusion", "rerank")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("model-compare")

app = FastAPI(title="Model Compare PoC")
_cache = {"t": 0.0, "data": {}}
_hits = defaultdict(deque)
_daily = {"day": "", "n": 0}


def auth():
    return {"Authorization": f"Bearer {KEY}"}


def guard():
    if not KEY:
        log.error("MODEL_ACCESS_KEY is not set")
        raise HTTPException(500, "The server is not configured correctly.")


def client_ip(request: Request) -> str:
    if CLIENT_IP_HEADER:
        value = request.headers.get(CLIENT_IP_HEADER, "").split(",")[0].strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request):
    """Per-client limit plus a global ceiling, so a spoofed or shared IP can't cause unbounded spend.
    In-memory: resets on restart and is per instance, so run a single instance."""
    now = time.time()
    if len(_hits) > 5000:                                   # drop stale keys
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            del _hits[k]
    keys = ((client_ip(request), RATE_LIMIT), ("__global__", GLOBAL_RATE_LIMIT))
    for key, limit in keys:
        q = _hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many comparisons right now. Try again in a minute.")
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if _daily["day"] != today:
        _daily.update(day=today, n=0)
    if DAILY_LIMIT and _daily["n"] >= DAILY_LIMIT:
        raise HTTPException(429, "The daily demo limit has been reached. Please try again tomorrow.")
    for key, _ in keys:
        _hits[key].append(now)
    _daily["n"] += 1


def friendly_error(status: Optional[int] = None, timeout: bool = False) -> str:
    """Generic, safe messages. Provider details go to server logs only."""
    if timeout or status in (408, 504):
        return "The model took too long to respond."
    if status in (401, 403):
        return "The server is not authorised to use this model."
    if status == 404:
        return "This model is not available."
    if status == 429:
        return "The model provider is rate limiting requests. Try again shortly."
    if status == 400:
        return "The model rejected this request."
    if status and status >= 500:
        return "The model provider had an error. Try again."
    return "The request to this model failed."


async def get_models() -> dict:
    if _cache["data"] and time.time() - _cache["t"] < 300:
        return _cache["data"]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/models", headers=auth())
    if r.status_code != 200:
        log.warning("model list failed: status=%s body=%s", r.status_code, r.text[:500])
        raise HTTPException(502, "Could not load the model list.")
    data = {}
    for m in r.json().get("data", []):
        mid = m.get("id", "")
        if not mid or any(s in mid.lower() for s in NON_CHAT):
            continue
        data[mid] = {"id": mid, "owner": m.get("owned_by", ""),
                     "context_length": m.get("context_length"),
                     "max_output_tokens": m.get("max_output_tokens")}
    _cache.update(t=time.time(), data=data)
    return data


@app.get("/api/config")
async def config():
    return {"max_models": MAX_MODELS, "token_cap": TOKEN_CAP}


@app.get("/api/models")
async def models():
    guard()
    return sorted((await get_models()).values(), key=lambda m: (m["owner"], m["id"]))


class Compare(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    models: list[str] = Field(min_length=1, max_length=20)
    max_tokens: int = Field(default=2000, ge=50)


async def _post(client, model, prompt, param, limit):
    return await client.post(f"{BASE}/chat/completions", headers=auth(), json={
        "model": model, "messages": [{"role": "user", "content": prompt}], param: limit})


async def ask_one(client, model, prompt, limit) -> dict:
    start = time.time()
    base = {"model": model, "token_limit": limit}
    try:
        r = await _post(client, model, prompt, "max_completion_tokens", limit)
        if r.status_code == 400 and "max_completion_tokens" in r.text:   # recognised compat error
            r = await _post(client, model, prompt, "max_tokens", limit)
        latency = round(time.time() - start, 2)
        if r.status_code >= 400:
            log.warning("upstream error model=%s status=%s body=%s", model, r.status_code, r.text[:500])
            return {**base, "status": "error", "latency": latency,
                    "error": friendly_error(r.status_code), "error_code": f"upstream_{r.status_code}"}
        data = r.json()
        choice = data["choices"][0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return {**base, "status": "success" if text.strip() else "empty", "latency": latency,
                "text": text, "finish_reason": choice.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens")}
    except httpx.TimeoutException:
        log.warning("timeout model=%s", model)
        return {**base, "status": "error", "latency": round(time.time() - start, 2),
                "error": friendly_error(timeout=True), "error_code": "timeout"}
    except Exception:
        log.exception("unexpected failure model=%s", model)
        return {**base, "status": "error", "latency": round(time.time() - start, 2),
                "error": friendly_error(), "error_code": "internal"}


@app.post("/api/compare")
async def compare(body: Compare, request: Request):
    guard()
    rate_limit(request)
    chosen = list(dict.fromkeys(body.models))
    if len(chosen) > MAX_MODELS:
        raise HTTPException(400, f"Select at most {MAX_MODELS} models.")
    valid = await get_models()
    unknown = [m for m in chosen if m not in valid]
    if unknown:
        raise HTTPException(400, "One or more selected models are unavailable.")

    async def run():
        async with httpx.AsyncClient(timeout=120) as client:
            tasks = []
            for m in chosen:
                limit = min(body.max_tokens, TOKEN_CAP, valid[m]["max_output_tokens"] or TOKEN_CAP)
                tasks.append(asyncio.create_task(ask_one(client, m, body.prompt, limit)))
            try:
                for fut in asyncio.as_completed(tasks):
                    yield json.dumps(await fut) + "\n"
            finally:
                # Client disconnected (or we finished): stop any model calls still in flight.
                pending = [t for t in tasks if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    log.info("cancelled %d in-flight model call(s)", len(pending))
                    await asyncio.gather(*pending, return_exceptions=True)

    return StreamingResponse(run(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/", StaticFiles(directory="static", html=True), name="static")
