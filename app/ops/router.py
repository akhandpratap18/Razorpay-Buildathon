from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.db.connection import get_db_pool
from app.ops.chat_agent import get_ops_agent
from app.ops.tools import AskHumanException
from app.rate_limit import check_rate_limit

ops_router = APIRouter(tags=["ops"])
log = structlog.get_logger(__name__)


def verify_ops_token(x_ops_token: str = Header(None)) -> str:
    from app.config import settings

    if not x_ops_token or x_ops_token != settings.ops_api_token:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid Ops Token")
    return x_ops_token


class OpsChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] = []  # [{"role": "user", "content": "hello"}, ...]


@ops_router.post("/ops/chat")
async def ops_chat(
    req: OpsChatRequest,
    request: Request,
    token: str = Depends(verify_ops_token),
    _rate_limit: None = Depends(check_rate_limit),
) -> dict[str, Any]:
    """Ops Chat Agent endpoint."""
    import re

    # Detect if the CURRENT user message contains Hindi (Devanagari script)
    is_hindi = bool(re.search(r"[\u0900-\u097F]", req.message))

    try:
        agent = get_ops_agent()

        from langchain_core.messages import AIMessage, HumanMessage

        messages: list[Any] = []
        for msg in req.history:
            if msg["role"] == "user":
                # Strip any prior language directive we injected to avoid bleed into future turns
                clean = re.sub(
                    r"\s*\[RESPOND IN (ENGLISH|HINDI)[^\]]*\]\s*$", "", msg["content"]
                ).strip()
                messages.append(HumanMessage(content=clean))
            elif msg["role"] == "agent":
                messages.append(AIMessage(content=msg["content"]))

        # Always explicitly tell the agent which language to use based on CURRENT message only
        if is_hindi:
            lang_directive = "[RESPOND IN HINDI — user message is in Hindi. Translate ALL prose to Hindi. Keep Transaction IDs, amounts, and status codes in English.]"
        else:
            lang_directive = "[RESPOND IN ENGLISH — the user's current message is in English. Reply in English regardless of any previous Hindi messages in this conversation.]"

        messages.append(HumanMessage(content=f"{req.message}\n\n{lang_directive}"))
        log.info("ops_chat.history", history=messages, is_hindi=is_hindi)

        result = await agent.ainvoke({"messages": messages})
        final_message = result["messages"][-1].content

        # Post-processing: if user wrote in Hindi but reply drifted to English, translate it
        if is_hindi and not re.search(r"[\u0900-\u097F]", final_message):
            try:
                from langchain_core.messages import HumanMessage as HMsg
                from langchain_groq import ChatGroq
                from pydantic import SecretStr

                from app.config import settings

                translator_llm = ChatGroq(
                    api_key=SecretStr(settings.groq_api_key),
                    model=settings.groq_fallback_model,
                    temperature=0,
                )
                translation_prompt = (
                    "Translate the following text into Hindi. "
                    "Keep ALL technical values exactly as-is (Transaction IDs like 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', "
                    "INR amounts, status codes like 'failed'/'recovered'/'escalated', "
                    "date strings like '2026-08-27', URLs, and markdown table structure with '|' characters). "
                    "Only translate the human-readable prose and headings.\n\n"
                    f"TEXT:\n{final_message}"
                )
                translation_result = await translator_llm.ainvoke(
                    [HMsg(content=translation_prompt)]
                )
                final_message = translation_result.content
                log.info("ops_chat.translated_to_hindi")
            except Exception as te:
                log.warning("ops_chat.translation_failed", error=str(te))

        return {"reply": final_message}
    except AskHumanException as e:
        return {
            "reply": e.prompt,
            "input_request": {"prompt": e.prompt, "options": e.options},
        }
    except Exception as e:
        log.error("ops.chat_error", error=str(e))
        return {"reply": "Sorry, ops chat is currently unavailable."}


@ops_router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@ops_router.get("/ops/transactions")
async def get_transactions(
    filter: str = "all",
    token: str = Depends(verify_ops_token),
) -> list[dict[str, Any]]:
    """Return transactions filtered by status."""
    async with get_db_pool() as conn:
        query = """
            SELECT DISTINCT ON (t.id)
                t.id, t.amount_inr, t.category, t.status,
                t.created_at, t.email, t.contact, t.error_reason,
                COALESCE(p.promised_leg_inr, 0) as promised_leg_inr,
                p.due_date, p.status as promise_status,
                EXISTS(SELECT 1 FROM audit_log a WHERE a.transaction_id::uuid = t.id AND a.event_type = 'customer_requested_escalation') as is_customer_initiated,
                EXISTS(SELECT 1 FROM promise_to_pay p2 WHERE p2.transaction_id::uuid = t.id AND p2.status IN ('pending', 'reminded')) as has_active_promise
            FROM transactions t
            LEFT JOIN promise_to_pay p
                ON p.transaction_id::uuid = t.id
                AND p.status IN ('broken', 'pending', 'reminded')
        """

        args = []
        if filter != "all":
            query += " WHERE t.status = $1"
            if filter == "escalated":
                query += " OR p.status = 'broken'"
            args.append(filter)

        query += " ORDER BY t.id, t.created_at DESC LIMIT 200"

        rows = await conn.fetch(query, *args)
        return [
            {
                "id": str(r["id"]),
                "amount_inr": float(r["amount_inr"]),
                "category": r["category"] or "unknown",
                "status": r["status"],
                "error_reason": r["error_reason"] or "",
                "email": r["email"] or "",
                "created_at": r["created_at"].isoformat(),
                "promised_leg_inr": float(r["promised_leg_inr"]),
                "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                "is_customer_initiated": r["is_customer_initiated"],
            }
            for r in rows
        ]


@ops_router.get("/ops/check-db")
async def check_db() -> dict[str, Any]:
    async with get_db_pool() as conn:
        onetime = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE recovery_link_url IS NOT NULL"
        )
        promise_paid = await conn.fetchval(
            "SELECT COUNT(*) FROM promise_to_pay WHERE status = 'paid'"
        )
        total_tx = await conn.fetchval("SELECT COUNT(*) FROM transactions")
        return {
            "recovery_link_url_count": onetime,
            "promise_paid": promise_paid,
            "total": total_tx,
        }


@ops_router.get("/ops/batch-summary")
async def get_batch_summary(token: str = Depends(verify_ops_token)) -> dict[str, Any]:
    """Return recovery metrics for the batch (judging view)."""
    from app.ops.metrics import compute_recovery_metrics

    async with get_db_pool() as conn:
        try:
            metrics = await compute_recovery_metrics(conn)

            total_tx = metrics["total_transactions"]
            statuses = metrics["statuses"]
            categories = metrics["categories"]
            total_recovered_inr = metrics["total_recovered_inr"]
            amount_pending = metrics["amount_pending"]
            amount_at_risk = metrics["amount_at_risk"]
            # Audit trail samples
            sample_audits = await conn.fetch("""
                SELECT t.id, t.amount_inr, t.category, t.status, t.created_at,
                       (SELECT json_agg(a) FROM (
                           SELECT event_type, actor, created_at, payload
                           FROM audit_log
                           WHERE transaction_id = t.id
                           ORDER BY created_at ASC
                       ) a) as trail
                FROM transactions t
                WHERE t.status IN ('recovered', 'escalated', 'killed')
                ORDER BY t.created_at DESC
                LIMIT 5
                """)

            samples = []
            for row in sample_audits:
                import json

                samples.append(
                    {
                        "id": str(row["id"]),
                        "amount_inr": float(row["amount_inr"]),
                        "category": row["category"],
                        "status": row["status"],
                        "created_at": row["created_at"].isoformat(),
                        "trail": json.loads(row["trail"]) if row["trail"] else [],
                    }
                )

            return {
                "metrics": {
                    "total_transactions": total_tx,
                    "total_recovered_inr": total_recovered_inr,
                    "amount_pending": float(amount_pending or 0),
                    "amount_at_risk": float(amount_at_risk or 0),
                    "status_breakdown": statuses,
                    "category_breakdown": categories,
                    "fraud_clean_stops": categories.get("fraud_hard_stop", 0),
                },
                "samples": samples,
            }
        finally:
            await conn.close()


@ops_router.get("/dev/get_token/{payment_id}")
async def dev_get_token(payment_id: str) -> dict[str, Any]:
    async with get_db_pool() as conn:
        row = await conn.fetchrow(
            "SELECT id, recovery_token FROM transactions WHERE payment_id = $1",
            payment_id,
        )
        if not row:
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        return {"id": row["id"], "recovery_token": row["recovery_token"]}


@ops_router.get("/ops/audit/{transaction_id}")
async def get_audit_trail(
    transaction_id: str, token: str = Depends(verify_ops_token)
) -> dict[str, Any]:
    """Return raw audit logs, chat history, and transaction details for the timeline modal."""
    async with get_db_pool() as conn:
        tx = await conn.fetchrow(
            "SELECT * FROM transactions WHERE id = $1::uuid", transaction_id
        )
        if not tx:
            raise HTTPException(404, "Transaction not found")

        audit_rows = await conn.fetch(
            "SELECT * FROM audit_log WHERE transaction_id = $1::uuid ORDER BY created_at ASC",
            transaction_id,
        )

        chat_row = await conn.fetchrow(
            "SELECT messages FROM chat_history WHERE recovery_token = $1",
            tx["recovery_token"],
        )

        promise_row = await conn.fetchrow(
            "SELECT * FROM promise_to_pay WHERE transaction_id = $1", transaction_id
        )

        return {
            "transaction": dict(tx),
            "promise": dict(promise_row) if promise_row else None,
            "audit_trail": [dict(r) for r in audit_rows],
            "chat_history": (
                __import__("json").loads(chat_row["messages"])
                if chat_row and chat_row["messages"]
                else []
            ),
        }
