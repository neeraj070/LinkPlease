# FAILURES.md — Known Failure Modes & Architectural Trade-offs

This document outlines the specific failure modes, edge case vulnerabilities, and architectural trade-offs present in this LinkPlease implementation.

---

### 1. Process Crash Between Mock API Acceptance & SQLite Commit
* **Condition**: If the process receives a `202 Accepted` response from `POST /v1/dm/send`, but the server is forcibly killed (`SIGKILL`, OOM, or power loss) before `database.update_dm_accepted()` commits to SQLite.
* **Impact**: On restart, the DM status is reset from `sending` back to `pending`. When the dispatcher picks it up, it re-sends the request using the same attempt idempotency key (`dm:{user_id}:{rule_id}:attempt:1`). If the mock API maintained the idempotency key in memory and restarted during the outage, it may treat the retried request as a new send and deliver a duplicate DM.

### 2. Multi-Instance Deployment & Rate Limit Breaches (Horizontal Scaling Limitation)
* **Condition**: If this application is deployed as multiple horizontal instances behind a round-robin load balancer without a centralized state store.
* **Impact**: Each instance maintains its own local SQLite database and in-memory `SlidingWindowRateLimiter`. Simultaneous webhooks for the same user landing on different instances could pass duplicate user-rule checks concurrently. Additionally, combined outgoing requests from multiple instances would breach the 10 requests / 60 seconds platform rate limit.

### 3. Exhaustion of Max Retries During Prolonged Mock API Outages
* **Condition**: If the mock API experiences a sustained outage or error rate (returning HTTP 500 or network timeouts for several consecutive minutes).
* **Impact**: The dispatcher backoff mechanism will attempt retries up to `MAX_RETRIES` (default 3). Once `MAX_RETRIES` is reached, the DM status permanently transitions to `failed`. If the mock API recovers after 10 minutes, those DMs are not automatically retried and will remain in a permanent `failed` state.

### 4. `comment.deleted` Race Condition After Mock API Acceptance
* **Condition**: A `comment.created` event arrives and is sent to `POST /v1/dm/send`, receiving `202 Accepted` (`dm_accepted`). A `comment.deleted` event then arrives 1 second later while the DM is still queued on the mock API server.
* **Impact**: Our `record_deleted_comment` handler sets any `pending` or `sending` DMs to `cancelled`. However, because `POST /v1/dm/send` was already executed, the mock API will still attempt delivery of the DM to the user. The DM will eventually transition to `delivered` in reconciliation, even though the comment was deleted on Instagram.

---

### 5. Architectural Trade-offs & Production-Scale Enhancements

#### SQLite vs PostgreSQL Trade-off
- **Current Choice**: SQLite with WAL mode (`journal_mode=WAL`).
- **Rationale**: Zero external setup overhead, embedded single-file durability, sub-millisecond local reads/writes, perfect for single-instance deployments.
- **Production Choice**: PostgreSQL with transactional connection pooling (e.g., PgBouncer).
- **Rationale**: Supports high concurrent write throughput across multiple application workers without lock contention.

#### In-Memory Rate Limiter vs Distributed Rate Limiter
- **Current Choice**: In-memory `SlidingWindowRateLimiter` (`deque` with `asyncio.Lock`).
- **Rationale**: Simple, zero latency overhead, strictly enforces rate limits within a single process.
- **Production Choice**: Distributed rate limiter powered by Redis (e.g. Redis sorted sets `ZADD` / `ZREMRANGEBYSCORE` or generic Token Bucket).
- **Rationale**: Coordinates global rate limits across all distributed application instances to prevent platform API throttling.

#### Production Architecture Recommendations
In a production system processing millions of events daily:
1. **Message Broker**: Decouple webhook ingestion from DM processing using Kafka or RabbitMQ.
2. **Distributed Storage**: Use PostgreSQL for persistent event/DM records and Redis for distributed idempotency locks & rate limits.
3. **Dead Letter Queue (DLQ)**: Instead of marking DMs permanently `failed` after `MAX_RETRIES`, route them to a DLQ for manual inspection or automated replay upon platform API recovery.
4. **Distributed Tracing & Monitoring**: Instrument with OpenTelemetry, Prometheus, and Grafana for real-time observability.
