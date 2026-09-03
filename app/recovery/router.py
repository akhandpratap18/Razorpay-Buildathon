import typing
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.config import settings
from app.ops.tools import AskHumanException
from app.rate_limit import check_rate_limit

log = structlog.get_logger(__name__)

_hostile_counts: dict[str, int] = {}


def format_error_reason(reason: str | None) -> str:
    if not reason:
        return "an unknown issue"

    # Customer-friendly translations for common payment gateway errors
    friendly_reasons = {
        "recurring_charge_failed": "we couldn't process your automatic subscription payment",
        "card_limit_exceeded": "your card hit its limit or had insufficient balance",
        "insufficient_funds": "your account had insufficient balance",
        "bank_not_responding": "your bank's servers were temporarily down",
        "gateway_not_responding": "the payment gateway was temporarily down",
        "card_disabled_for_online_payments": "your card is not enabled for online transactions",
        "international_card_blocked": "your card does not support international transactions",
        "card_expired": "your card has expired",
        "payment_failed": "the transaction was declined by your bank",
        "subscription_halted": "your subscription was paused due to previous failures",
        "authentication_failed": "the payment authentication failed",
    }

    raw = str(reason).lower().strip()
    if raw in friendly_reasons:
        return friendly_reasons[raw]

    return raw.replace("_", " ")


recovery_router = APIRouter(tags=["recovery"])


@recovery_router.get("/recover/{token}/context")
async def get_recovery_session(
    token: str, request: Request, _rate_limit: None = Depends(check_rate_limit)
) -> dict[str, Any]:
    """Validate token and return transaction context for the chat UI."""
    import asyncpg

    conn = await asyncpg.connect(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        row = await conn.fetchrow(
            """
            SELECT id, amount_inr, category, error_reason, status
            FROM transactions
            WHERE recovery_token = $1
            """,
            token,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Link no longer active")

        if row["status"] in ("recovered", "escalated", "killed", "dead"):
            from app.audit.logger import AuditLogger

            await AuditLogger(conn).log_event(
                transaction_id=row["id"],
                event_type="rejected_access",
                actor="system",
                payload={"reason": "link_inactive", "status": row["status"]},
            )
            raise HTTPException(
                status_code=410, detail="This link is no longer active."
            )

        # Check if there's an active promise to pay
        promise = await conn.fetchrow(
            """
            SELECT promised_leg_inr, due_date
            FROM promise_to_pay
            WHERE transaction_id = $1 AND status IN ('pending', 'reminded')
            ORDER BY created_at DESC LIMIT 1
            """,
            str(row["id"]),
        )

        if promise:
            amount_display = f"₹{promise['promised_leg_inr']:,.2f}"
            opening_message = f"Your scheduled payment of {amount_display} is due soon. Do you want me to help you sort this out?"
        else:
            amount_display = f"₹{row['amount_inr']:,.2f}"
            reason = format_error_reason(row["error_reason"])
            opening_message = f"Your payment of {amount_display} failed because {reason}. Do you want me to help you sort this out?"

        return {
            "transaction_id": str(row["id"]),
            "amount_display": amount_display,
            "reason_display": row["error_reason"],
            "opening_message": opening_message,
        }
    finally:
        await conn.close()


class ChatRequest(BaseModel):
    message: str


@recovery_router.post("/recover/{token}/chat")
async def chat_recovery_session(
    token: str,
    req: ChatRequest,
    request: Request,
    _rate_limit: None = Depends(check_rate_limit),
) -> dict[str, Any]:
    """Conversational endpoint for recovery."""
    import json

    import asyncpg
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        messages_from_dict,
        messages_to_dict,
    )

    from app.recovery.customer_agent import get_customer_agent

    conn = await asyncpg.connect(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        row = await conn.fetchrow(
            "SELECT id, amount_inr, category, status, order_id, email, contact, error_reason FROM transactions WHERE recovery_token = $1",
            token,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Link no longer active")

        if row["status"] in ("recovered", "escalated", "killed", "dead"):
            from app.audit.logger import AuditLogger

            await AuditLogger(conn).log_event(
                transaction_id=row["id"],
                event_type="rejected_access",
                actor="system",
                payload={"reason": "link_inactive", "status": row["status"]},
            )
            raise HTTPException(status_code=410, detail="Link no longer active")

        amount_inr = row["amount_inr"]
        category = row["category"]
        transaction_id = str(row["id"])
        order_id = row["order_id"]
        email = row["email"]
        contact = row["contact"]

        promise = await conn.fetchrow(
            """
            SELECT promised_leg_inr, due_date
            FROM promise_to_pay
            WHERE transaction_id = $1 AND status IN ('pending', 'reminded')
            ORDER BY created_at DESC LIMIT 1
            """,
            str(row["id"]),
        )

        active_promise_text = ""
        has_active_promise = False
        if promise:
            has_active_promise = True
            due_str = promise["due_date"].strftime("%Y-%m-%d")
            active_promise_text = (
                f"⚠️ LOCKED PROMISE CONTEXT: This customer has an ACTIVE, LOCKED split-payment commitment. "
                f"They owe {promise['promised_leg_inr']} INR and committed to paying it by {due_str}. "
                f"YOU ARE NOT AUTHORISED TO OFFER ANY DATE EXTENSION OR RENEGOTIATION WHATSOEVER. "
                f"If they ask to push the date, delay, or pay less, tell them firmly: "
                f"'Your payment commitment is locked and cannot be changed. Please use the link sent to your email to pay by {due_str}.' "
                f"If they claim they cannot pay at all or get extremely frustrated, you MUST use `request_human_escalation`. Do not just say you escalated it — you must physically use the tool. "
                f"Do NOT call agree_to_split with a different date or amount. "
                f"The ONLY action you may take is helping them pay right now — if they say they want to pay NOW, tell them to use the link already sent to their email."
            )

        agent = get_customer_agent(
            transaction_id,
            amount_inr,
            order_id,
            email,
            contact,
            category,
            active_promise_text,
            has_active_promise,
            recovery_token=token,
            error_reason=format_error_reason(row["error_reason"]),
        )

        # Server-side memory management
        mem_row = await conn.fetchrow(
            "SELECT messages FROM chat_history WHERE recovery_token = $1", token
        )
        if mem_row and mem_row["messages"]:
            msgs_data = mem_row["messages"]
            if isinstance(msgs_data, str):
                msgs_data = json.loads(msgs_data)
            messages = messages_from_dict(msgs_data)
        else:
            # Reconstruct the opening message so the AI has context for "yes"
            amount_display = f"₹{amount_inr:,.2f}"
            reason = format_error_reason(row["error_reason"])
            opening_msg = f"Your payment of {amount_display} failed because {reason}. Do you want me to help you sort this out?"
            if promise:
                opening_msg = f"Your scheduled payment of ₹{promise['promised_leg_inr']:,.2f} is due soon. Do you want me to help you sort this out?"

            from langchain_core.messages import AIMessage

            messages = [AIMessage(content=opening_msg)]

        messages.append(HumanMessage(content=req.message))

        # Run the agent
        from app.recovery.sentiment import score_sentiment

        sentiment = await score_sentiment(req.message)

        if sentiment == "hostile":
            _hostile_counts[token] = _hostile_counts.get(token, 0) + 1
            if _hostile_counts[token] >= 2:
                await conn.execute(
                    "UPDATE transactions SET status = 'escalated' WHERE recovery_token = $1",
                    token,
                )
                return {
                    "reply": "I've noted your concern and flagged this for our collections team. You will hear from us within 24 hours."
                }
            response = await agent.ainvoke(
                {"messages": messages},
                config={"configurable": {"thread_id": transaction_id}},
            )
        else:
            _hostile_counts.pop(token, None)
            response = await agent.ainvoke(
                {"messages": messages},
                config={"configurable": {"thread_id": transaction_id}},
            )

        new_history_json = json.dumps(messages_to_dict(response["messages"][-20:]))
        await conn.execute(
            """
            INSERT INTO chat_history (recovery_token, messages)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (recovery_token) DO UPDATE SET messages = EXCLUDED.messages
            """,
            token,
            new_history_json,
        )
        final_msg = response["messages"][-1].content
        return {"reply": final_msg}

    except AskHumanException as e:
        return {
            "reply": e.prompt,
            "input_request": {"prompt": e.prompt, "options": e.options},
        }
    finally:
        await conn.close()


@recovery_router.get("/voice/speak")
async def generate_speech(text: str, lang: str = "en") -> Any:
    """Generate high-quality TTS audio via Sarvam (if configured), Kokoro, or gTTS fallback."""
    import io
    import re
    from datetime import datetime

    # Pre-process text to make dates readable for TTS (e.g. 2026-08-31 -> 31 August 2026)
    def _format_date_for_speech(match: re.Match[str]) -> str:
        try:
            d = datetime.strptime(match.group(0), "%Y-%m-%d")
            return d.strftime("%d %B %Y")
        except Exception:
            return match.group(0)

    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", _format_date_for_speech, text)

    import httpx
    from fastapi.responses import StreamingResponse

    from app.config import settings

    # 1. SARVAM AI (Best for Hinglish) - Free Tier Available
    if hasattr(settings, "sarvam_api_key") and settings.sarvam_api_key:
        try:
            from sarvamai import AsyncSarvamAI

            sarvam_client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
            target_lang = "hi-IN"
            if lang == "en" and not re.search(r"[\u0900-\u097F\u0600-\u06FF]", text):
                target_lang = "en-IN"

            async def proxy_sarvam() -> typing.AsyncGenerator[bytes, None]:
                response = sarvam_client.text_to_speech.convert(
                    text=text,
                    language_code=target_lang,
                    speaker="shubh",
                    model="bulbul:v3",
                )
                import base64

                # Handling both sync/async return models for robustness
                res = await response if hasattr(response, "__await__") else response

                # Check if it has 'audios' (array of base64) or 'audio'
                if hasattr(res, "audios") and res.audios:
                    yield base64.b64decode(res.audios[0])
                elif hasattr(res, "audio"):
                    yield base64.b64decode(res.audio)
                elif isinstance(res, dict) and "audios" in res:
                    yield base64.b64decode(res["audios"][0])
                else:
                    raise ValueError(f"Unknown response format: {res}")

            return StreamingResponse(proxy_sarvam(), media_type="audio/wav")
        except Exception as e:
            log.warning("sarvam_api_failed", error=str(e))
            pass

    # 2. KOKORO EXTERNAL SERVER
    if hasattr(settings, "kokoro_tts_url") and settings.kokoro_tts_url:
        kokoro_url = f"{settings.kokoro_tts_url.rstrip('/')}/voice/speak"
        try:

            async def proxy_kokoro() -> typing.AsyncGenerator[bytes, None]:
                async with (
                    httpx.AsyncClient() as client,
                    client.stream(
                        "GET", kokoro_url, params={"text": text, "lang": lang}
                    ) as response,
                ):
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk

            return StreamingResponse(proxy_kokoro(), media_type="audio/wav")
        except Exception as e:
            log.warning("kokoro_proxy_failed", error=str(e))
            pass

    # 3. GOOGLE TTS FALLBACK (100% Free, 0 RAM, Always works)
    try:
        from gtts import gTTS

        target_lang = (
            "hi"
            if re.search(r"[\u0900-\u097F\u0600-\u06FF]", text)
            or lang.startswith("hi")
            or lang.startswith("ur")
            else "en"
        )
        tts = gTTS(text=text, lang=target_lang)

        # Save to memory
        mp3_fp = io.BytesIO()
        tts.write_to_fp(mp3_fp)
        mp3_fp.seek(0)

        def iterfile() -> typing.Generator[bytes, None, None]:
            yield mp3_fp.read()

        return StreamingResponse(iterfile(), media_type="audio/mpeg")
    except Exception as e:
        log.error("gtts_generation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"TTS Error: {str(e)}") from e


@recovery_router.post("/voice/transcribe")
async def transcribe_audio(
    request: Request,
    audio: UploadFile = File(...),
    prompt_context: str = Form("Transcribe the audio exactly as spoken."),
    _rate_limit: None = Depends(check_rate_limit),
) -> dict[str, Any]:
    """Transcribe audio using Sarvam AI (preferred) or Groq Whisper API (fallback)."""
    import httpx

    file_bytes = await audio.read()
    filename = audio.filename or "audio.webm"

    # 1. SARVAM AI (Better for Indic languages/Hinglish)
    if hasattr(settings, "sarvam_api_key") and settings.sarvam_api_key:
        try:
            from sarvamai import AsyncSarvamAI

            sarvam_client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)

            response = await sarvam_client.speech_to_text.transcribe(
                file=(filename, file_bytes),
                model="saaras:v3",
                mode="transcribe",
                language_code="Unknown",
            )
            res = await response if hasattr(response, "__await__") else response

            if hasattr(res, "transcript") and res.transcript:
                return {"transcript": res.transcript}

        except Exception as e:
            log.warning("sarvam_stt_failed", error=str(e))
            # Fall back to Groq below

    # 2. GROQ WHISPER (Fallback)
    if not settings.groq_api_key:
        raise HTTPException(status_code=500, detail="No STT API key configured")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                data={
                    "model": "whisper-large-v3",
                    "response_format": "verbose_json",
                    "prompt": prompt_context,
                },
                files={
                    "file": (
                        filename,
                        file_bytes,
                        audio.content_type or "audio/webm",
                    )
                },
            )

            if response.status_code != 200:
                log.error(
                    "groq_whisper_error",
                    status=response.status_code,
                    response=response.text,
                )
                raise HTTPException(
                    status_code=502, detail="Failed to transcribe audio"
                )

            data = response.json()

            segments = data.get("segments", [])
            if segments:
                avg_logprob = sum(seg.get("avg_logprob", 0) for seg in segments) / len(
                    segments
                )
                if avg_logprob < -0.6:
                    log.warning(
                        "whisper_low_confidence",
                        avg_logprob=avg_logprob,
                        text=data.get("text"),
                    )
                    raise HTTPException(status_code=400, detail="low_confidence")

            return {"transcript": data.get("text", "")}
    except Exception as e:
        log.error("transcription_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error") from e
