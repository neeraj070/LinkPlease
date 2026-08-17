import os
import hmac
import hashlib
import json
import pytest
from fastapi.testclient import TestClient

# Use temporary test database
os.environ["DB_PATH"] = "test_linkplease.db"
os.environ["PSEUDOGRAM_API_KEY"] = "test_secret_key"

from app.main import app
from app import database

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

def test_webhook_duplicate_event_id():
    # Create rule first
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
    # Create rule
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
    # The DM was cancelled so queued becomes 0 and failed becomes 1
    assert stats["queued"] == 0
    assert stats["failed"] == 1
