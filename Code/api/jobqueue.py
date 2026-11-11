"""Background processing.

QUEUE_MODE=rq      jobs go to Redis; run `rq worker docintel` (see docker-compose.yml / README)
QUEUE_MODE=thread  jobs run in a thread pool inside the API process (no Redis needed)
QUEUE_MODE=sync    jobs run immediately in the request (tests)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import config

_pool = None
QUEUE_NAME = "docintel"


def enqueue(document_id: int) -> str:
    mode = config.settings.queue_mode
    from .processing import process_document
    if mode == "sync":
        process_document(document_id)
        return "sync"
    if mode == "rq":
        from redis import Redis
        from rq import Queue, Retry
        q = Queue(QUEUE_NAME, connection=Redis.from_url(config.settings.redis_url))
        job = q.enqueue("api.processing.process_document", document_id, job_timeout=3600,
                        retry=Retry(max=1, interval=[10]), result_ttl=86400, failure_ttl=7 * 86400)
        return job.id
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=config.settings.thread_workers, thread_name_prefix="docintel")
    _pool.submit(process_document, document_id)
    return "thread"


def queue_info() -> dict:
    mode = config.settings.queue_mode
    info = {"mode": mode}
    if mode == "rq":
        try:
            from redis import Redis
            from rq import Queue, Worker
            conn = Redis.from_url(config.settings.redis_url)
            q = Queue(QUEUE_NAME, connection=conn)
            info.update(redis="ok", queued=q.count, workers=len(Worker.all(connection=conn)))
        except Exception as e:  # noqa: BLE001
            info.update(redis=f"error: {e}")
    return info
