import asyncio
import hmac
import hashlib
import uuid
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException, status
from pydantic import BaseModel

from app import config
from app import database
from app import worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("linkplease.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing database and starting background tasks...")
    database.init_db()

    dispatcher_task = asyncio.create_task(worker.start_dispatcher_loop())
    reconciliation_task = asyncio.create_task(worker.start_reconciliation_loop())

    yield

    # Shutdown
    logger.info("Shutting down background tasks...")
    dispatcher_task.cancel()
    reconciliation_task.cancel()
    await asyncio.gather(dispatcher_task, reconciliation_task, return_exceptions=True)
    logger.info("Shutdown complete.")

app = FastAPI(title="LinkPlease Engine", lifespan=lifespan)

class CreateRuleRequest(BaseModel):
    keyword: str
    dm_message: str

def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    api_key = (getattr(config, "PSEUDOGRAM_API_KEY", "") or "").strip().strip("'\"")
    if not api_key:
        logger.info("HMAC verification skipped: PSEUDOGRAM_API_KEY is not set.")
        return True
    if not signature_header:
        logger.warning("Rejecting webhook: Missing X-PseudoGram-Signature header. Body len: %d", len(raw_body))
        return False

    expected_sig = hmac.new(
        api_key.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest().lower()

    sig_to_check = signature_header.strip().strip("'\"")
    if sig_to_check.lower().startswith("sha256="):
        sig_to_check = sig_to_check[7:]
    elif sig_to_check.lower().startswith("sha256:"):
        sig_to_check = sig_to_check[7:]

    sig_to_check = sig_to_check.strip().lower()

    is_valid = hmac.compare_digest(expected_sig, sig_to_check)

    sig_header_prefix = signature_header[:10] if signature_header else ""
    computed_prefix = expected_sig[:10] if expected_sig else ""
    received_prefix = sig_to_check[:10] if sig_to_check else ""
    logger.info(
        "HMAC verify: body_len=%d, header_prefix='%s...', computed_prefix='%s...', received_prefix='%s...', match=%s",
        len(raw_body), sig_header_prefix, computed_prefix, received_prefix, is_valid
    )

    return is_valid


@app.post("/webhook")
async def webhook_endpoint(request: Request):
    raw_body = await request.body()
    sig_header = request.headers.get("X-PseudoGram-Signature", "")

    if not verify_signature(raw_body, sig_header):
        logger.warning("Rejecting webhook due to invalid HMAC signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    if not event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing event_id")

    # Step 1: Deduplicate event_id
    is_new = database.try_register_event(event_id)
    if not is_new:
        logger.info(f"Duplicate event_id received: {event_id}. Ignored.")
        return {"ok": True, "status": "duplicate_event_ignored"}

    # Step 2: Handle event types
    if event_type == "comment.created":
        comment_id = data.get("comment_id", "")
        text = data.get("text", "")
        from_user = data.get("from", {})
        user_id = from_user.get("user_id", "")

        if comment_id and text and user_id:
            database.process_comment_created(event_id, text, user_id, comment_id)

    elif event_type == "comment.deleted":
        comment_id = data.get("comment_id", "")
        if comment_id:
            database.record_deleted_comment(comment_id)

    return {"ok": True}

@app.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(req: CreateRuleRequest):
    rule_id = f"rule_{uuid.uuid4().hex[:8]}"
    created_rule = database.add_rule(rule_id, req.keyword, req.dm_message)
    return created_rule

@app.get("/stats")
async def get_stats():
    return database.get_stats()


                                                                                    
