import time
import json
import random
import hmac
import hashlib
import urllib.request
import urllib.error
import sys

BASE_URL = "http://127.0.0.1:8000"
API_KEY = "bWVrYWxhbmVlcmFqa3VtYXJAZ21haWwuY29t.6c9ee69d7f0c332ae285"

def compute_sig(body: bytes, secret: str = API_KEY) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def post_json(url: str, data: dict, headers: dict = None) -> dict:
    if headers is None:
        headers = {}
    headers["Content-Type"] = "application/json"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTPError {e.code}: {e.read().decode('utf-8')}")
        raise

def get_json(url: str, headers: dict = None) -> dict:
    if headers is None:
        headers = {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))

def run_local_stress_test():
    print("--- Starting Local Stress Test ---")
    
    # 1. Create Rules
    rule1 = post_json(f"{BASE_URL}/rules", {"keyword": "PRICE", "dm_message": "Price list link"})
    rule2 = post_json(f"{BASE_URL}/rules", {"keyword": "LINK", "dm_message": "Resource link"})
    print(f"Created rules: {rule1['rule_id']}, {rule2['rule_id']}")

    users = [f"usr_{i:03d}" for i in range(1, 51)] # 50 unique users
    keywords = ["PRICE", "LINK", "OTHER", "price", "link"]

    print("Sending 500 events over 10 seconds...")
    start_time = time.time()
    events_sent = 0

    for i in range(500):
        user_id = random.choice(users)
        kw = random.choice(keywords)
        comment_id = f"cmt_{i:04d}"
        
        # 8% chance of duplicate event_id
        if i > 10 and random.random() < 0.08:
            event_id = f"evt_{random.randint(0, i-1):04d}"
        else:
            event_id = f"evt_{i:04d}"

        # 5% chance of comment.deleted
        if random.random() < 0.05 and i > 5:
            del_comment_id = f"cmt_{random.randint(0, i-1):04d}"
            payload = {
                "event_id": f"evt_del_{i}",
                "event_type": "comment.deleted",
                "sent_at": "2026-08-10T09:14:22Z",
                "data": {"comment_id": del_comment_id}
            }
        else:
            payload = {
                "event_id": event_id,
                "event_type": "comment.created",
                "sent_at": "2026-08-10T09:14:22Z",
                "data": {
                    "comment_id": comment_id,
                    "post_id": "post_999",
                    "text": f"Hey please send me {kw} info!",
                    "from": {"user_id": user_id, "username": f"user_{user_id}"}
                }
            }

        body = json.dumps(payload).encode("utf-8")
        sig = compute_sig(body)
        
        req = urllib.request.Request(f"{BASE_URL}/webhook", data=body, headers={
            "Content-Type": "application/json",
            "X-PseudoGram-Signature": sig
        })
        try:
            with urllib.request.urlopen(req) as resp:
                pass
        except Exception as e:
            print(f"Webhook error on event {i}: {e}")

        events_sent += 1
        elapsed = time.time() - start_time
        target_elapsed = (i + 1) * (10.0 / 500.0)
        if elapsed < target_elapsed:
            time.sleep(target_elapsed - elapsed)

    total_time = time.time() - start_time
    print(f"Sent {events_sent} events in {total_time:.2f} seconds.")

    print("\nPolling stats live...")
    for _ in range(5):
        stats = get_json(f"{BASE_URL}/stats")
        print(f"Current Stats: {stats}")
        time.sleep(2)

if __name__ == "__main__":
    run_local_stress_test()
