import asyncio
import time
import logging
from collections import deque
import httpx

from app.config import PSEUDOGRAM_API_KEY, PSEUDOGRAM_BASE_URL, MAX_RETRIES
from app import database

logger = logging.getLogger("linkplease.worker")

class SlidingWindowRateLimiter:
    """
    Enforces a strict sliding-window rate limit.
    Default: max 9 requests per 60 seconds (safe buffer under the 10 req/60s limit).
    """
    def __init__(self, max_requests: int = 9, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            # Remove timestamps outside the sliding window
            while self.timestamps and now - self.timestamps[0] >= self.window_seconds:
                self.timestamps.popleft()

            if len(self.timestamps) >= self.max_requests:
                sleep_time = self.window_seconds - (now - self.timestamps[0]) + 0.05
                if sleep_time > 0:
                    logger.warning(f"Rate limiter threshold reached ({len(self.timestamps)} requests in last {self.window_seconds}s). Sleeping for {sleep_time:.2f}s...")
                    await asyncio.sleep(sleep_time)
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.window_seconds:
                    self.timestamps.popleft()

            self.timestamps.append(now)

rate_limiter = SlidingWindowRateLimiter(max_requests=9, window_seconds=60.0)

async def start_dispatcher_loop():
    logger.info("Starting DM Dispatcher Loop...")
    database.reset_unfinished_dms()

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                dm = database.get_next_pending_dm()
                if not dm:
                    await asyncio.sleep(0.2)
                    continue

                await rate_limiter.acquire()

                url = f"{PSEUDOGRAM_BASE_URL}/v1/dm/send"
                headers = {
                    "X-API-Key": PSEUDOGRAM_API_KEY,
                    "Idempotency-Key": dm["idempotency_key"],
                    "Content-Type": "application/json"
                }
                payload = {
                    "recipient_user_id": dm["user_id"],
                    "message": dm["message"],
                    "comment_id": dm["comment_id"]
                }

                logger.info(f"Sending DM for DM_DB_ID {dm['id']} (attempt {dm['attempt_number']}, idempotency_key={dm['idempotency_key']})")

                try:
                    response = await client.post(url, json=payload, headers=headers)
                    status_code = response.status_code

                    if status_code in (200, 202):
                        res_json = response.json()
                        dm_id = res_json.get("dm_id", "")
                        logger.info(f"DM_DB_ID {dm['id']} accepted by mock API -> dm_id={dm_id}")
                        database.update_dm_accepted(dm["id"], dm_id)

                    elif status_code == 429:
                        retry_after_str = response.headers.get("Retry-After", "60")
                        try:
                            retry_after = float(retry_after_str)
                        except ValueError:
                            retry_after = 60.0
                        logger.warning(f"Received 429 Rate Limited from mock API. Backing off for {retry_after}s...")
                        database.reset_dm_to_pending(dm["id"])
                        await asyncio.sleep(retry_after)

                    elif status_code == 400:
                        logger.error(f"Received 400 Invalid Request for DM_DB_ID {dm['id']}: {response.text}")
                        database.update_dm_status(dm["id"], "failed")

                    else: # 500 or unexpected status code
                        logger.warning(f"Received {status_code} for DM_DB_ID {dm['id']}: {response.text}")
                        if dm["attempt_number"] < MAX_RETRIES:
                            await asyncio.sleep(1.0 * dm["attempt_number"])
                            database.reset_dm_to_pending(dm["id"])
                        else:
                            database.update_dm_status(dm["id"], "failed")

                except httpx.RequestError as exc:
                    logger.error(f"Network error sending DM_DB_ID {dm['id']}: {exc}")
                    if dm["attempt_number"] < MAX_RETRIES:
                        await asyncio.sleep(1.0 * dm["attempt_number"])
                        database.reset_dm_to_pending(dm["id"])
                    else:
                        database.update_dm_status(dm["id"], "failed")

            except asyncio.CancelledError:
                logger.info("DM Dispatcher Loop cancelled.")
                break
            except Exception as e:
                logger.exception(f"Unexpected error in dispatcher loop: {e}")
                await asyncio.sleep(1.0)

async def start_reconciliation_loop():
    logger.info("Starting Status Reconciliation Loop...")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                accepted_dms = database.get_accepted_dms()
                for dm in accepted_dms:
                    dm_db_id = dm["id"]
                    dm_id = dm["dm_id"]
                    if not dm_id:
                        continue

                    url = f"{PSEUDOGRAM_BASE_URL}/v1/dm/{dm_id}"
                    headers = {"X-API-Key": PSEUDOGRAM_API_KEY}

                    try:
                        res = await client.get(url, headers=headers)
                        if res.status_code == 200:
                            data = res.json()
                            dm_status = data.get("status", "queued")
                            logger.debug(f"Reconciliation poll for dm_id={dm_id}: status={dm_status}")

                            if dm_status == "delivered":
                                logger.info(f"DM_DB_ID {dm_db_id} (dm_id={dm_id}) delivered!")
                                database.update_dm_status(dm_db_id, "delivered")

                            elif dm_status == "failed":
                                logger.warning(f"DM_DB_ID {dm_db_id} (dm_id={dm_id}) status is failed on mock API.")
                                current_attempt = dm["attempt_number"]
                                if current_attempt < MAX_RETRIES:
                                    new_attempt = current_attempt + 1
                                    new_key = f"dm:{dm['user_id']}:{dm['rule_id']}:attempt:{new_attempt}"
                                    logger.info(f"Re-queueing DM_DB_ID {dm_db_id} for attempt {new_attempt} with key {new_key}")
                                    database.requeue_dm_for_retry(dm_db_id, new_attempt, new_key)
                                else:
                                    logger.error(f"DM_DB_ID {dm_db_id} exhausted max retries ({MAX_RETRIES}). Marking failed.")
                                    database.update_dm_status(dm_db_id, "failed")

                    except httpx.RequestError as exc:
                        logger.error(f"Network error checking status for dm_id {dm_id}: {exc}")

                await asyncio.sleep(2.5)

            except asyncio.CancelledError:
                logger.info("Reconciliation Loop cancelled.")
                break
            except Exception as e:
                logger.exception(f"Unexpected error in reconciliation loop: {e}")
                await asyncio.sleep(2.0)
