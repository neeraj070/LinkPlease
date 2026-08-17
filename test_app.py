import os
import hmac
import hashlib
import json
import asyncio
import pytest
from fastapi.testclient import TestClient

# Use temporary test database
os.environ["DB_PATH"] = "test_linkplease.db"
os.environ["PSEUDOGRAM_API_KEY"] = "test_secret_key"

from app.main import app
from app import database
from app.worker import SlidingWindowRateLimiter

def clear_db():
    with database.get_connection() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM rules")
        conn.execute("DELETE FROM dms")
        conn.execute("DELETE FROM duplicate_logs")
        conn.execute("DELETE FROM deleted_comments")
        conn.commit()

@pytest.fixture(autouse=True)
def setup_teardown_db():
    database.init_db()
    clear_db()
    yield
    clear_db()

client = TestClient(app)

def compute_sig(body: bytes, secret: str = "test_secret_key") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def test_rule_creation():
    res = client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here is the price list!"})
    assert res.status_code == 201
    data = res.json()
    assert "rule_id" in data
    assert data["keyword"] == "PRICE"
    assert data["dm_message"] == "Here is the price list!"

def test_webhook_invalid_signature():
    payload = {"event_id": "evt_1", "event_type": "comment.created", "data": {}}
    body = json.dumps(payload).encode()
    res = client.post("/webhook", content=body, headers={"X-PseudoGram-Signature": "sha256=invalid"})
    assert res.status_code == 401

def test_webhook_missing_signature():
    payload = {"event_id": "evt_missing_sig", "event_type": "comment.created", "data": {}}
    body = json.dumps(payload).encode()
    # Missing X-PseudoGram-Signature header when PSEUDOGRAM_API_KEY is configured
    res = client.post("/webhook", content=body)
    assert res.status_code == 401

def test_webhook_malformed_payload():
    # Invalid JSON (with valid signature header for the raw body)
    raw_invalid = b"invalid json body"
    sig_invalid = compute_sig(raw_invalid)
    res1 = client.post("/webhook", content=raw_invalid, headers={"X-PseudoGram-Signature": sig_invalid, "Content-Type": "application/json"})
    assert res1.status_code == 400

    # Missing event_id
    payload_no_evt = {"event_type": "comment.created", "data": {}}
    body_no_evt = json.dumps(payload_no_evt).encode()
    res2 = client.post("/webhook", content=body_no_evt, headers={"X-PseudoGram-Signature": compute_sig(body_no_evt)})
    assert res2.status_code == 400

    # Null data field
    payload_null_data = {"event_id": "evt_null_data", "event_type": "comment.created", "data": None}
    body_null_data = json.dumps(payload_null_data).encode()
    res3 = client.post("/webhook", content=body_null_data, headers={"X-PseudoGram-Signature": compute_sig(body_null_data)})
    assert res3.status_code == 200

def test_webhook_duplicate_event_id():
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Price message"})

    payload = {
        "event_id": "evt_100",
        "event_type": "comment.created",
        "sent_at": "2026-08-10T09:14:22.481Z",
        "data": {
            "comment_id": "cmt_1",
            "post_id": "post_1",
            "text": "PRICE please",
            "from": {"user_id": "usr_1", "username": "user1"}
        }
    }
    body = json.dumps(payload).encode()
    sig = compute_sig(body)

    # First delivery
    res1 = client.post("/webhook", content=body, headers={"X-PseudoGram-Signature": sig})
    assert res1.status_code == 200

    # Second delivery (duplicate event_id)
    res2 = client.post("/webhook", content=body, headers={"X-PseudoGram-Signature": sig})
    assert res2.status_code == 200
    assert res2.json()["status"] == "duplicate_event_ignored"

    stats = client.get("/stats").json()
    assert stats["duplicates_blocked"] == 1

def test_user_rule_deduplication():
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Price list"})

    # Event 1: User 1 comments PRICE
    payload1 = {
        "event_id": "evt_201",
        "event_type": "comment.created",
        "data": {
            "comment_id": "cmt_201",
            "text": "Send me PRICE",
            "from": {"user_id": "usr_200", "username": "user2"}
        }
    }
    body1 = json.dumps(payload1).encode()
    client.post("/webhook", content=body1, headers={"X-PseudoGram-Signature": compute_sig(body1)})

    # Event 2: Same User 200 comments PRICE again with different event_id & comment_id
    payload2 = {
        "event_id": "evt_202",
        "event_type": "comment.created",
        "data": {
            "comment_id": "cmt_202",
            "text": "What is the PRICE?",
            "from": {"user_id": "usr_200", "username": "user2"}
        }
    }
    body2 = json.dumps(payload2).encode()
    client.post("/webhook", content=body2, headers={"X-PseudoGram-Signature": compute_sig(body2)})

    stats = client.get("/stats").json()
    assert stats["queued"] == 1
    assert stats["duplicates_blocked"] == 1

def test_comment_deleted():
    client.post("/rules", json={"keyword": "DISCOUNT", "dm_message": "Coupon code"})

    # Comment created
    p_created = {
        "event_id": "evt_301",
        "event_type": "comment.created",
        "data": {
            "comment_id": "cmt_301",
            "text": "Give me DISCOUNT",
            "from": {"user_id": "usr_300", "username": "user3"}
        }
    }
    body_c = json.dumps(p_created).encode()
    client.post("/webhook", content=body_c, headers={"X-PseudoGram-Signature": compute_sig(body_c)})

    # Comment deleted before dispatch
    p_deleted = {
        "event_id": "evt_302",
        "event_type": "comment.deleted",
        "data": {"comment_id": "cmt_301"}
    }
    body_d = json.dumps(p_deleted).encode()
    client.post("/webhook", content=body_d, headers={"X-PseudoGram-Signature": compute_sig(body_d)})

    stats = client.get("/stats").json()
    assert stats["queued"] == 0
    assert stats["failed"] == 1

def test_rate_limiter_sliding_window():
    async def _test():
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=1.0)
        start = asyncio.get_event_loop().time()
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()
        # 4th acquire must wait until window expires
        await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed >= 0.95
    asyncio.run(_test())

def test_worker_requeue_and_retry_exhaustion():
    # Insert a dummy DM into database
    client.post("/rules", json={"keyword": "TEST", "dm_message": "Test msg"})
    database.process_comment_created("evt_r1", "TEST me", "usr_retry", "cmt_r1")

    # Get DM
    dm = database.get_next_pending_dm()
    assert dm is not None
    dm_id_db = dm["id"]

    # Mark accepted
    database.update_dm_accepted(dm_id_db, "dm_mock_123")
    accepted = database.get_accepted_dms()
    assert len(accepted) == 1

    # Simulate reconciliation retry attempt 2
    database.requeue_dm_for_retry(dm_id_db, 2, "dm:usr_retry:rule_1:attempt:2")
    dm2 = database.get_next_pending_dm()
    assert dm2["attempt_number"] == 2
    assert dm2["idempotency_key"] == "dm:usr_retry:rule_1:attempt:2"

    # Simulate retry exhaustion (attempt 3 reaches limit)
    database.update_dm_status(dm_id_db, "failed")
    stats = client.get("/stats").json()
    assert stats["failed"] == 1
