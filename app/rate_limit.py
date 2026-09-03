import time

from fastapi import HTTPException, Request

RATE_LIMITS: dict[str, dict[str, float]] = {}
MAX_TOKENS = 30
REFILL_RATE_PER_SECOND = 2


def check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"

    now = time.time()

    if ip not in RATE_LIMITS:
        RATE_LIMITS[ip] = {"tokens": MAX_TOKENS, "last_updated": now}

    state = RATE_LIMITS[ip]
    elapsed = now - state["last_updated"]

    state["tokens"] = min(
        MAX_TOKENS, state["tokens"] + elapsed * REFILL_RATE_PER_SECOND
    )
    state["last_updated"] = now

    if state["tokens"] < 1:
        raise HTTPException(status_code=429, detail="Too Many Requests")

    state["tokens"] -= 1
