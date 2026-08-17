# LinkPlease — Auto-DM Engine for Instagram Comments

LinkPlease is a lightweight, fault-tolerant auto-DM engine built with FastAPI and SQLite. It processes incoming comment webhooks, enforces idempotency and rate limits, and dispatches automated DMs via the Pseudogram API.

---

## Architecture Overview

```
                        +---------------------------+
                        |  Incoming Webhook Request |
                        +-------------+-------------+
                                      |
                                      v
                       +--------------+--------------+
                       |  HMAC-SHA256 Signature Check |
                       +--------------+--------------+
                                      |
                                      v
                       +--------------+--------------+
                       |  Event ID Deduplication DB  |
                       +--------------+--------------+
                                      |
                                      v
                       +--------------+--------------+
                       |  User-Rule Deduplication DB |
                       +--------------+--------------+
                                      |
                                      v (Queued in SQLite)
                  +-------------------+-------------------+
                  |                                       |
                  v                                       v
      +-----------+-----------+               +-----------+-----------+
      |  DM Dispatcher Loop   |               | Reconciliation Loop   |
      | (Sliding Window Limit)|               | (Poll Status & Retry) |
      +-----------+-----------+               +-----------+-----------+
                  |                                       |
                  +-------------------+-------------------+
                                      |
                                      v
                        +-------------+-------------+
                        |   Pseudogram Mock API     |
                        +---------------------------+
```

---

## API Endpoints

### 1. `POST /webhook`
Receives comment events (`comment.created`, `comment.deleted`).
- **Headers**: Requires `X-PseudoGram-Signature` matching HMAC-SHA256 of raw body using `PSEUDOGRAM_API_KEY`.
- **Response**: `200 OK` on valid ingestion/duplicate drop, `401 Unauthorized` on bad/missing signature, `400 Bad Request` on malformed payload.

### 2. `POST /rules`
Creates a keyword rule for automated DMs.
- **Request Body**: `{"keyword": "PRICE", "dm_message": "Check out our pricing!"}`
- **Response**: `201 Created` with generated `rule_id`.

### 3. `GET /stats`
Retrieves live processing metrics derived directly from database counters.
- **Response**: `{"sent": 12, "failed": 1, "queued": 0, "duplicates_blocked": 5}`

---

## Technical Specifications

### Webhook Flow & Security
1. Raw HTTP request body is captured prior to JSON parsing.
2. If `PSEUDOGRAM_API_KEY` is configured, HMAC-SHA256 hex digest of the body is verified using `hmac.compare_digest`. Requests with missing or incorrect signature headers are rejected with `401 Unauthorized`.
3. Ingested events undergo JSON verification. If payload formatting or `data` structures are malformed, `400 Bad Request` is returned.

### Idempotency Strategy
- **Event-Level**: `events` table enforces `PRIMARY KEY (event_id)`. Duplicate webhook deliveries with identical `event_id` are caught, logged into `duplicate_logs` as `duplicate_event`, and return `{"ok": true, "status": "duplicate_event_ignored"}` without duplicate processing.
- **User-Rule-Level**: `dms` table enforces `UNIQUE (user_id, rule_id)`. If a user comments matching keywords multiple times, subsequent DMs for that rule are blocked, logged as `duplicate_user_rule`, and metric `duplicates_blocked` is incremented.
- **Attempt-Level**: Outgoing DM API calls send attempt-scoped idempotency keys: `dm:{user_id}:{rule_id}:attempt:{attempt_number}`.

### Rate Limiting Strategy
- An in-memory `SlidingWindowRateLimiter` enforces a 9 requests / 60 seconds threshold (buffering under Pseudogram's strict 10 req/60s limit).
- If downstream HTTP `429 Too Many Requests` is returned by Pseudogram, the worker parses the `Retry-After` header and pauses sending.

### Worker & Reconciliation System
- **Dispatcher Loop**: Asynchronously selects `pending` DMs, enforces sliding window rate limits, and posts to `POST /v1/dm/send`.
- **Status Transitions**:
  - Initial insert -> `pending`
  - Picked by dispatcher -> `sending`
  - Downstream 200/202 Accepted -> `dm_accepted` (stores `dm_id`)
  - Status check returns delivered -> `delivered`
  - Downstream 500 or delivery failure -> retry up to `MAX_RETRIES` with key `dm:{user_id}:{rule_id}:attempt:{new_attempt}`.
- **Reconciliation Loop**: Polls `GET /v1/dm/{dm_id}` for `dm_accepted` items to catch asynchronous delivery completions or failures.
- **`comment.deleted`**: Instantly cancels any `pending` or `sending` DMs associated with the deleted comment.

### Persistence
- Uses SQLite with Write-Ahead Logging (`PRAGMA journal_mode=WAL;`) and `PRAGMA busy_timeout=30000;`.
- On application restart, `reset_unfinished_dms()` converts orphaned `sending` DMs back to `pending` so no messages are lost.

---

## Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `PSEUDOGRAM_API_KEY` | Secret key for HMAC signature verification and Pseudogram API headers | `""` |
| `PSEUDOGRAM_BASE_URL` | Base URL for Pseudogram API | `https://pseudogram-api.onrender.com` |
| `DB_PATH` | Path to SQLite database file | `linkplease.db` |
| `MAX_RETRIES` | Max attempt limit for failed DMs | `3` |
| `PORT` | Web server port | `8000` |

---

## Local Setup & Testing

### 1. Installation
```bash
pip install -r requirements.txt
```

### 2. Environment Setup
```bash
cp .env.example .env
```

### 3. Run Application
```bash
uvicorn app.main:app --reload --port 8000
```

### 4. Execute Test Suite
```bash
python -m pytest -v
```

### 5. Run High-Concurrency Stress Test
```bash
python test_simulation.py
```

---

## Deployment (Render)

The project includes a `Procfile` for Render web service deployments:
```
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

- Live URL: `https://linkplease-di4u.onrender.com/`

---

## Architectural Limitations & Known Trade-offs

See [`FAILURES.md`](file:///c:/Users/mekal/OneDrive/Desktop/LinkPlease/FAILURES.md) for full details on:
1. Process crashes between mock API acceptance & SQLite commit.
2. In-memory sliding window rate limiter limits under multi-instance horizontal scaling.
3. Retry exhaustion during prolonged external API outages.
4. `comment.deleted` race condition after external API acceptance.
