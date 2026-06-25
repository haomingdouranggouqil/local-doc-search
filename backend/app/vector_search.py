from __future__ import annotations

import json
import math
import signal
import threading
import time
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .database import Database

SILICONFLOW_PROVIDER = "siliconflow"
SILICONFLOW_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"
SILICONFLOW_UNAUTHORIZED_STATUSES = {401, 403}
SILICONFLOW_MAX_REQUESTS_PER_SECOND = 2000 / 60
SILICONFLOW_MODEL_MAX_INPUT_TOKENS = 32768
MODEL_CARD_QUERY_TASK = (
    "Given a search query, retrieve relevant passages from a local document library"
)
MODEL_READY_FILES = (
    "config.json",
    "modules.json",
    "tokenizer.json",
    "model.safetensors.index.json",
    "1_Pooling/config.json",
    "2_Dense/config.json",
)
VECTOR_REBUILD_DOCUMENT_INTERVAL = 100


class VectorSearchUnavailable(RuntimeError):
    pass


class VectorJobCancelled(RuntimeError):
    pass


class VectorRemoteUnavailable(RuntimeError):
    pass


class EmbeddingAPIHardTimeout(TimeoutError):
    pass


class VectorSearchService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.dimension = max(1, int(settings.embedding_dimension or 512))
        self.provider = SILICONFLOW_PROVIDER
        self.model_key = self._model_key()
        self.index_path = settings.vector_dir / "chunks.faiss"
        self.meta_path = settings.vector_dir / "chunks.faiss.json"
        self._model = None
        self._device = ""
        self._remote_ready = False
        self._model_lock = threading.RLock()
        self._index_lock = threading.RLock()
        self._checkpoint_lock = threading.RLock()
        self._rate_limit_lock = threading.Lock()
        self._next_siliconflow_request_at = 0.0
        self._token_window: deque[tuple[float, int]] = deque()
        self._token_window_total = 0
        self._http_client = None
        self._http_client_key = ""
        self._http_client_lock = threading.Lock()
        self._status_cache: dict[str, Any] | None = None
        self._status_cache_at = 0.0
        self._status_cache_ttl = 10.0

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not refresh
            and self._status_cache is not None
            and now - self._status_cache_at < self._status_cache_ttl
        ):
            return dict(self._status_cache)
        data = self.db.vector_stats(self.model_key, self.dimension)
        index_is_current = (
            data.get("index_model") == self.model_key
            and int(data.get("index_dim") or 0) == self.dimension
        )
        if not index_is_current:
            data["index_count"] = 0
            data["index_document_count"] = 0
            data["index_type"] = ""
            data["index_error"] = ""
        data["enabled"] = bool(self.settings.vector_index_enabled)
        data["available"] = (
            self.index_path.exists()
            and data["index_count"] > 0
            and index_is_current
            and int(data.get("embeddings") or 0) >= int(data.get("index_count") or 0)
        )
        data["model_dir"] = ""
        data["device"] = self._device or (
            "siliconflow-api" if self.settings.effective_siliconflow_api_key else "not configured"
        )
        data["provider"] = self.provider
        data["api_url"] = self._remote_embeddings_url()
        data["model"] = SILICONFLOW_EMBEDDING_MODEL
        data["api_key_configured"] = bool(self.settings.effective_siliconflow_api_key)
        data["request_rate_limit_per_second"] = self._request_rate_limit()
        data["tokens_per_minute_limit"] = self._tokens_per_minute_limit()
        data["embedding_batch_size"] = self._max_batch_items()
        data["embedding_concurrency"] = self._embedding_concurrency()
        data["max_request_tokens"] = self._max_request_tokens()
        data["quantization"] = "4bit"
        self._status_cache = dict(data)
        self._status_cache_at = now
        return data

    @property
    def model_dir(self) -> Path:
        return self.settings.vector_dir

    def enqueue_missing(self, force: bool = False, limit: int = 10000) -> int:
        if not self.settings.vector_index_enabled:
            raise VectorSearchUnavailable("vector index is disabled")
        queued = self.db.enqueue_vector_jobs(
            self.model_key,
            self.dimension,
            force=force,
            limit=limit,
        )
        if queued == 0:
            status = self.status(refresh=True)
            if status["embeddings"] > 0 and (status["index_dirty"] or not self.index_path.exists()):
                self.rebuild_faiss()
        return queued

    def process_document(self, document_id: str, job_id: int | None = None) -> None:
        if not self.settings.vector_index_enabled:
            if job_id is not None:
                self.db.update_job(
                    job_id,
                    status="failed",
                    progress=1,
                    message="Fuzzy index disabled",
                    error="vector index is disabled",
                )
            return

        doc = self.db.get_document(document_id)
        rel_path = doc["rel_path"] if doc else document_id
        try:
            if job_id is not None:
                self._raise_if_cancelled(job_id)
                self.db.update_job(job_id, progress=0.03, message=self._prepare_message())
            self.ensure_model()
            chunks = self.db.chunks_missing_embeddings(document_id, self.model_key, self.dimension)
            if not chunks:
                self._rebuild_on_checkpoint_or_last_vector_job(job_id)
                if job_id is not None:
                    self.db.update_job(job_id, status="done", progress=1, message="Fuzzy embeddings already ready")
                return

            if job_id is not None:
                self._raise_if_cancelled(job_id)
                self.db.update_job(job_id, progress=0.08, message=self._vectorizing_message())

            total = len(chunks)
            stored = 0
            processed = 0
            row_batches = self._pack_embedding_row_batches(chunks)
            with ThreadPoolExecutor(max_workers=min(self._embedding_concurrency(), len(row_batches))) as executor:
                future_to_batch = {
                    executor.submit(
                        self._embed_remote_batch,
                        [str(row["text"] or "").strip() for row in batch],
                        "document",
                    ): batch
                    for batch in row_batches
                }
                for future in as_completed(future_to_batch):
                    self._raise_if_cancelled(job_id)
                    batch = future_to_batch[future]
                    vectors = future.result()
                    payload = [
                        (int(row["id"]), vector.astype("<f4", copy=False).tobytes())
                        for row, vector in zip(batch, vectors, strict=False)
                    ]
                    stored += self.db.upsert_chunk_embeddings(
                        document_id,
                        self.model_key,
                        self.dimension,
                        payload,
                    )
                    processed += len(batch)
                    if job_id is not None:
                        progress = 0.08 + 0.78 * (min(processed, total) / max(total, 1))
                        self.db.update_job(
                            job_id,
                            progress=min(progress, 0.86),
                            message=f"{self._vectorizing_message()} {min(processed, total)}/{total}",
                        )

            rebuilt = self._rebuild_on_checkpoint_or_last_vector_job(job_id)
            if job_id is not None:
                message = "Fuzzy index checkpoint saved" if rebuilt else "Fuzzy embeddings ready; FAISS checkpoint deferred"
                self.db.update_job(job_id, status="done", progress=1, message=message)
            self.db.record_event("vector", f"Vectorized: {rel_path} ({stored} lines)", document_id, rel_path)
        except VectorJobCancelled as exc:
            if job_id is not None:
                self.db.update_job(
                    job_id,
                    status="cancelled",
                    progress=1,
                    message="Cancelled",
                    error=str(exc),
                )
            self.db.record_event("cancel", f"Cancelled fuzzy index: {rel_path}", document_id, rel_path)
        except VectorRemoteUnavailable as exc:
            if job_id is not None:
                paused = self.db.pause_vector_jobs(str(exc))
                self.db.record_event(
                    "vector_pause",
                    f"Paused fuzzy index queue: embedding API unavailable; paused {paused} jobs",
                    document_id,
                    rel_path,
                )
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if job_id is not None:
                self.db.update_job(job_id, status="failed", progress=1, message="Fuzzy index failed", error=error)
            status = self.db.vector_stats(self.model_key, self.dimension)
            index_is_current_model = (
                status.get("index_model") == self.model_key
                and int(status.get("index_dim") or 0) == self.dimension
                and int(status.get("embeddings") or 0) >= int(status.get("index_count") or 0)
            )
            self.db.save_vector_index_state(
                count=status.get("index_count", 0) if index_is_current_model else 0,
                index_type=status.get("index_type", "") if index_is_current_model else "",
                model=self.model_key,
                dim=self.dimension,
                document_count=status.get("index_document_count", 0) if index_is_current_model else 0,
                error=error,
            )
            self.db.record_event("vector_error", f"Fuzzy index failed: {rel_path}; {error}", document_id, rel_path)
            raise

    def ensure_model(self) -> Path:
        self._ensure_remote_api()
        return self.settings.vector_dir

    def embed_query(self, query: str):
        return self._embed_remote([str(query or "").strip()], input_type="query")[0]

    def embed_documents(self, texts: list[str]):
        return self._embed_remote([str(text or "").strip() for text in texts], input_type="document")

    def _embed(self, inputs: list[str]):
        raise VectorSearchUnavailable("local embedding mode is disabled; configure SiliconFlow API Key")

    def _embed_remote(self, inputs: list[str], input_type: str):
        if not inputs:
            return self._empty_vectors()
        self.ensure_model()
        batches = self._pack_embedding_batches(inputs)
        concurrency = self._embedding_concurrency()
        if len(batches) == 1 or concurrency == 1:
            vectors = [self._embed_remote_batch(batch, input_type) for batch in batches]
        else:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(batches))) as executor:
                vectors = list(executor.map(lambda batch: self._embed_remote_batch(batch, input_type), batches))

        import numpy as np

        return np.vstack(vectors).astype("float32", copy=False)

    def _embed_remote_batch(self, inputs: list[str], input_type: str):
        try:
            response = self._post_remote_embeddings(inputs, input_type)
        except VectorSearchUnavailable as exc:
            if len(inputs) <= 1 or not remote_error_can_split(str(exc)):
                raise
            midpoint = max(1, len(inputs) // 2)
            left = self._embed_remote_batch(inputs[:midpoint], input_type)
            right = self._embed_remote_batch(inputs[midpoint:], input_type)
            import numpy as np

            return np.vstack([left, right]).astype("float32", copy=False)

        data = response.get("data")
        if not isinstance(data, list):
            raise VectorSearchUnavailable("embedding API response missing data")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        embeddings = [item.get("embedding") for item in ordered]
        if len(embeddings) != len(inputs):
            raise VectorSearchUnavailable(
                f"embedding API returned {len(embeddings)} vectors for {len(inputs)} inputs"
            )
        return self._coerce_vectors(embeddings)

    def _post_remote_embeddings(self, inputs: list[str], input_type: str) -> dict[str, Any]:
        try:
            import httpx
        except ModuleNotFoundError as exc:
            raise VectorSearchUnavailable("httpx is required for SiliconFlow embedding mode") from exc

        api_key = self.settings.effective_siliconflow_api_key
        if not api_key:
            raise VectorSearchUnavailable("SiliconFlow API Key is required for fuzzy search")
        payload: dict[str, Any] = {
            "model": SILICONFLOW_EMBEDDING_MODEL,
            "input": inputs,
            "dimensions": self.dimension,
            "encoding_format": "float",
            "truncate": "right",
        }

        attempts = max(1, int(self.settings.siliconflow_retries or 1))
        timeout = max(10.0, float(self.settings.siliconflow_timeout_seconds or 60))
        estimated_tokens = self._estimate_batch_tokens(inputs)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                http_timeout = httpx.Timeout(
                    timeout,
                    connect=min(10.0, timeout),
                    read=timeout,
                    write=min(30.0, timeout),
                    pool=min(10.0, timeout),
                )
                self._wait_for_siliconflow_slot(estimated_tokens)
                with remote_api_hard_timeout(timeout + 5):
                    response = self._remote_client(api_key).post(
                        self._remote_embeddings_url(),
                        json=payload,
                        timeout=http_timeout,
                    )
                if response.status_code >= 400:
                    detail = f"SiliconFlow embedding API returned HTTP {response.status_code}: {response.text[:300]}"
                    if response.status_code in SILICONFLOW_UNAUTHORIZED_STATUSES:
                        raise VectorSearchUnavailable(
                            "SiliconFlow API Key is invalid or unauthorized"
                        )
                    if remote_status_is_temporarily_unavailable(response.status_code):
                        raise VectorRemoteUnavailable(detail)
                    raise VectorSearchUnavailable(detail)
                return response.json()
            except Exception as exc:
                if remote_error_is_oom(str(exc)):
                    raise
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(min(8, 1.5 * (attempt + 1)))
        if isinstance(last_error, VectorRemoteUnavailable):
            raise last_error
        raise VectorSearchUnavailable(f"SiliconFlow embedding API failed: {last_error}") from last_error

    def _coerce_vectors(self, embeddings):
        import numpy as np

        vectors = np.asarray(embeddings, dtype="float32")
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.shape[1] < self.dimension:
            raise VectorSearchUnavailable(
                f"embedding dimension {vectors.shape[1]} is smaller than requested {self.dimension}"
            )
        vectors = vectors[:, : self.dimension].astype("float32", copy=False)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    def _empty_vectors(self):
        import numpy as np

        return np.empty((0, self.dimension), dtype="float32")

    def _pack_embedding_batches(self, inputs: list[str]) -> list[list[str]]:
        max_items = self._max_batch_items()
        max_tokens = self._max_request_tokens()
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for value in inputs:
            text = str(value or "").strip()
            tokens = self._estimate_text_tokens(text)
            would_exceed_items = max_items > 0 and len(current) >= max_items
            would_exceed_tokens = current and current_tokens + tokens > max_tokens
            if would_exceed_items or would_exceed_tokens:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(text)
            current_tokens += tokens
        if current:
            batches.append(current)
        return batches

    def _request_rate_limit(self) -> float:
        configured = float(self.settings.siliconflow_requests_per_second or 0)
        if configured <= 0:
            return 0.0
        return min(configured, SILICONFLOW_MAX_REQUESTS_PER_SECOND)

    def _tokens_per_minute_limit(self) -> int:
        return max(0, int(self.settings.siliconflow_tokens_per_minute or 0))

    def _max_batch_items(self) -> int:
        return max(0, int(self.settings.siliconflow_embedding_batch_size or 0))

    def _embedding_concurrency(self) -> int:
        return max(1, int(self.settings.siliconflow_embedding_concurrency or 120))

    def _max_request_tokens(self) -> int:
        configured = max(1, int(self.settings.siliconflow_max_request_tokens or 30000))
        return min(configured, SILICONFLOW_MODEL_MAX_INPUT_TOKENS)

    def _estimate_batch_tokens(self, inputs: list[str]) -> int:
        return sum(self._estimate_text_tokens(text) for text in inputs)

    def _estimate_text_tokens(self, text: str) -> int:
        # Conservative approximation for Chinese-heavy text: one Unicode codepoint is
        # treated as about one token, with a small per-input overhead.
        return min(self._max_request_tokens(), max(1, len(str(text or "").strip()) + 4))

    def _wait_for_siliconflow_slot(self, estimated_tokens: int) -> None:
        rate = self._request_rate_limit()
        token_limit = self._tokens_per_minute_limit()
        interval = 1.0 / rate if rate > 0 else 0.0
        tokens = max(1, int(estimated_tokens or 1))
        while True:
            wait_seconds = 0.0
            with self._rate_limit_lock:
                now = time.monotonic()
                self._prune_token_window(now)
                if interval > 0:
                    wait_seconds = max(wait_seconds, self._next_siliconflow_request_at - now)
                if token_limit > 0 and tokens <= token_limit and self._token_window_total + tokens > token_limit:
                    oldest = self._token_window[0][0] if self._token_window else now
                    wait_seconds = max(wait_seconds, 60.0 - (now - oldest))
                if wait_seconds <= 0:
                    if interval > 0:
                        self._next_siliconflow_request_at = max(now, self._next_siliconflow_request_at) + interval
                    if token_limit > 0:
                        self._token_window.append((now, tokens))
                        self._token_window_total += tokens
                    return
            time.sleep(min(max(wait_seconds, 0.01), 1.0))

    def _prune_token_window(self, now: float) -> None:
        cutoff = now - 60.0
        while self._token_window and self._token_window[0][0] <= cutoff:
            _, tokens = self._token_window.popleft()
            self._token_window_total = max(0, self._token_window_total - tokens)

    def _load_model(self, force_device: str | None = None):
        raise VectorSearchUnavailable("local embedding mode is disabled; configure SiliconFlow API Key")

    def _drop_model(self) -> None:
        with self._model_lock:
            self._model = None
            self._device = ""

    def _select_device(self, force_device: str | None = None) -> str:
        return "siliconflow-api"

    def _embedding_provider(self) -> str:
        return SILICONFLOW_PROVIDER

    def _use_remote_api(self) -> bool:
        return True

    def _model_key(self) -> str:
        return f"{SILICONFLOW_EMBEDDING_MODEL}@{self.dimension}:{SILICONFLOW_PROVIDER}"

    def _document_batch_size(self) -> int:
        item_cap = self._max_batch_items()
        if item_cap > 0:
            return item_cap * self._embedding_concurrency()
        return 0

    def _document_batch_token_budget(self) -> int:
        max_tokens = self._max_request_tokens()
        token_limit = self._tokens_per_minute_limit()
        if token_limit > 0:
            requests_needed = max(1, math.ceil(token_limit / max_tokens))
            requests_per_window = max(1, min(self._embedding_concurrency(), requests_needed))
            return max(max_tokens, min(token_limit, max_tokens * requests_per_window))
        requests_per_window = max(1, min(self._embedding_concurrency(), 60))
        return max_tokens * requests_per_window

    def _document_chunk_windows(self, chunks: list[Any]) -> list[list[Any]]:
        token_budget = self._document_batch_token_budget()
        fallback_item_budget = self._document_batch_size()
        windows: list[list[Any]] = []
        current: list[Any] = []
        current_tokens = 0
        for chunk in chunks:
            text = ""
            try:
                text = str(chunk["text"] or "")
            except (KeyError, TypeError):
                text = str(getattr(chunk, "text", "") or "")
            tokens = self._estimate_text_tokens(text)
            would_exceed_tokens = current and current_tokens + tokens > token_budget
            would_exceed_items = fallback_item_budget > 0 and len(current) >= fallback_item_budget
            if would_exceed_tokens or would_exceed_items:
                windows.append(current)
                current = []
                current_tokens = 0
            current.append(chunk)
            current_tokens += tokens
        if current:
            windows.append(current)
        return windows

    def _pack_embedding_row_batches(self, rows: list[Any]) -> list[list[Any]]:
        max_items = self._max_batch_items()
        max_tokens = self._max_request_tokens()
        batches: list[list[Any]] = []
        current: list[Any] = []
        current_tokens = 0
        for row in rows:
            try:
                text = str(row["text"] or "").strip()
            except (KeyError, TypeError):
                text = str(getattr(row, "text", "") or "").strip()
            tokens = self._estimate_text_tokens(text)
            would_exceed_items = max_items > 0 and len(current) >= max_items
            would_exceed_tokens = current and current_tokens + tokens > max_tokens
            if would_exceed_items or would_exceed_tokens:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(row)
            current_tokens += tokens
        if current:
            batches.append(current)
        return batches

    def _remote_client(self, api_key: str):
        import httpx

        with self._http_client_lock:
            if self._http_client is not None and self._http_client_key == api_key:
                return self._http_client
            if self._http_client is not None:
                self._http_client.close()
            connection_count = max(1, self._embedding_concurrency())
            self._http_client = httpx.Client(
                limits=httpx.Limits(
                    max_connections=connection_count,
                    max_keepalive_connections=connection_count,
                    keepalive_expiry=120,
                ),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                trust_env=False,
            )
            self._http_client_key = api_key
            return self._http_client

    def _prepare_message(self) -> str:
        return "Preparing SiliconFlow API"

    def _vectorizing_message(self) -> str:
        return "Calling SiliconFlow API"

    def _ensure_remote_api(self) -> None:
        if self._remote_ready:
            return
        if not self.settings.effective_siliconflow_api_key:
            raise VectorSearchUnavailable("SiliconFlow API Key is required for fuzzy search")
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError as exc:
            raise VectorSearchUnavailable("httpx is required for SiliconFlow embedding mode") from exc
        self._remote_ready = True
        self._device = "siliconflow-api"

    def _remote_base_url(self) -> str:
        url = self.settings.siliconflow_base_url.strip().rstrip("/")
        if url.endswith("/embeddings"):
            return url[: -len("/embeddings")]
        return url or "https://api.siliconflow.cn/v1"

    def _remote_embeddings_url(self) -> str:
        return f"{self._remote_base_url()}/embeddings"

    def _remote_health_url(self) -> str:
        return self._remote_base_url()

    def _rebuild_on_checkpoint_or_last_vector_job(self, job_id: int | None) -> bool:
        status = self.status(refresh=True)
        embedded_documents = int(status.get("embedded_documents") or 0)
        checkpoint_documents = int(status.get("index_document_count") or 0)
        checkpoint_due = embedded_documents - checkpoint_documents >= VECTOR_REBUILD_DOCUMENT_INTERVAL
        is_last_job = job_id is None or self.db.active_vector_job_count(exclude_job_id=job_id) == 0
        if not checkpoint_due and not is_last_job:
            return False

        acquired = self._checkpoint_lock.acquire(blocking=is_last_job)
        if not acquired:
            return False
        try:
            status = self.status(refresh=True)
            embedded_documents = int(status.get("embedded_documents") or 0)
            checkpoint_documents = int(status.get("index_document_count") or 0)
            checkpoint_due = embedded_documents - checkpoint_documents >= VECTOR_REBUILD_DOCUMENT_INTERVAL
            is_last_job = job_id is None or self.db.active_vector_job_count(exclude_job_id=job_id) == 0
            if not checkpoint_due and not is_last_job:
                return False
            if job_id is not None:
                self._raise_if_cancelled(job_id)
                message = "Saving FAISS fuzzy index" if is_last_job else "Saving FAISS checkpoint"
                self.db.update_job(job_id, progress=0.9, message=message)
            self._rebuild_faiss_unlocked(job_id=job_id, document_count=embedded_documents)
            self._status_cache = None
            return True
        finally:
            self._checkpoint_lock.release()

    def rebuild_faiss(self, job_id: int | None = None, document_count: int | None = None) -> None:
        with self._checkpoint_lock:
            self._rebuild_faiss_unlocked(job_id=job_id, document_count=document_count)
            self._status_cache = None

    def _rebuild_faiss_unlocked(
        self, job_id: int | None = None, document_count: int | None = None
    ) -> None:
        if document_count is None:
            document_count = int(self.db.vector_stats(self.model_key, self.dimension).get("embedded_documents") or 0)
        rows = self.db.embedding_rows(self.model_key, self.dimension)
        if not rows:
            self.index_path.unlink(missing_ok=True)
            self.meta_path.unlink(missing_ok=True)
            self.db.save_vector_index_state(
                count=0,
                index_type="empty",
                model=self.model_key,
                dim=self.dimension,
                document_count=0,
            )
            return

        self._raise_if_cancelled(job_id)
        try:
            import faiss
            import numpy as np
        except ModuleNotFoundError as exc:
            raise VectorSearchUnavailable("faiss-cpu and numpy are required for fuzzy search") from exc

        ids = np.asarray([int(row["chunk_id"]) for row in rows], dtype="int64")
        vectors = np.vstack(
            [np.frombuffer(row["vector"], dtype="<f4", count=self.dimension) for row in rows]
        ).astype("float32", copy=False)
        faiss.normalize_L2(vectors)

        with self._index_lock:
            index_type = "scalar-4bit"
            try:
                base = faiss.IndexScalarQuantizer(
                    self.dimension,
                    faiss.ScalarQuantizer.QT_4bit,
                    faiss.METRIC_INNER_PRODUCT,
                )
                base.train(vectors)
            except Exception:
                index_type = "flat-fallback"
                base = faiss.IndexFlatIP(self.dimension)
            index = faiss.IndexIDMap2(base)
            index.add_with_ids(vectors, ids)
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            faiss.write_index(index, str(self.index_path))
            self.meta_path.write_text(
                json.dumps(
                    {
                        "count": int(index.ntotal),
                        "document_count": int(document_count or 0),
                        "dimension": self.dimension,
                        "model": self.model_key,
                        "index_type": index_type,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.db.save_vector_index_state(
                count=int(index.ntotal),
                index_type=index_type,
                model=self.model_key,
                dim=self.dimension,
                document_count=int(document_count or 0),
            )

    def search_groups_page(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        scope: str | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, int(limit))
        start = max(0, int(offset))
        hits = self.search(query, scope=scope, top_k=self._candidate_count(page_size, start))
        grouped: dict[str, dict[str, Any]] = {}
        for hit in hits:
            group = grouped.get(hit["document_id"])
            if group is None:
                group = dict(hit)
                group["match_count"] = 0
                grouped[hit["document_id"]] = group
            group["match_count"] += 1
        groups = list(grouped.values())
        results = groups[start : start + page_size + 1]
        has_more = len(results) > page_size
        page_results = results[:page_size]
        return {
            "groups": page_results,
            "count": len(page_results),
            "returned_count": len(page_results),
            "offset": start,
            "limit": page_size,
            "has_more": has_more,
            "next_offset": start + len(page_results) if has_more else None,
        }

    def search_document_page(
        self,
        query: str,
        document_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        page_size = max(1, int(limit))
        start = max(0, int(offset))
        rows = self.db.document_embedding_rows(document_id, self.model_key, self.dimension)
        if not rows:
            return empty_page(page_size, start)

        import numpy as np

        query_vector = self.embed_query(query).astype("float32", copy=False)
        vectors = np.vstack(
            [np.frombuffer(row["vector"], dtype="<f4", count=self.dimension) for row in rows]
        ).astype("float32", copy=False)
        scores = vectors @ query_vector
        order = np.argsort(-scores)
        selected = order[start : start + page_size + 1]
        chunk_ids = [int(rows[int(index)]["chunk_id"]) for index in selected]
        row_map = self.db.rows_for_chunk_ids(chunk_ids, document_id=document_id)
        results: list[dict[str, Any]] = []
        for index in selected:
            chunk_id = int(rows[int(index)]["chunk_id"])
            row = row_map.get(chunk_id)
            if row is None:
                continue
            results.append(self._fuzzy_row(row, float(scores[int(index)])))
        has_more = len(results) > page_size
        page_results = results[:page_size]
        return {
            "results": page_results,
            "count": len(page_results),
            "returned_count": len(page_results),
            "offset": start,
            "limit": page_size,
            "has_more": has_more,
            "next_offset": start + len(page_results) if has_more else None,
        }

    def search(self, query: str, scope: str | None = None, top_k: int = 200) -> list[dict[str, Any]]:
        if not self.status()["available"]:
            raise VectorSearchUnavailable("fuzzy index is not built")
        try:
            import faiss
            import numpy as np
        except ModuleNotFoundError as exc:
            raise VectorSearchUnavailable("faiss-cpu and numpy are required for fuzzy search") from exc

        query_vector = self.embed_query(query).astype("float32", copy=False).reshape(1, self.dimension)
        with self._index_lock:
            index = faiss.read_index(str(self.index_path))
            distances, ids = index.search(query_vector, max(1, int(top_k)))
        ordered_ids = [int(item) for item in ids[0].tolist() if int(item) >= 0]
        scores = {int(chunk_id): float(score) for chunk_id, score in zip(ids[0], distances[0], strict=False)}
        row_map = self.db.rows_for_chunk_ids(ordered_ids, scope=scope)
        results: list[dict[str, Any]] = []
        for chunk_id in ordered_ids:
            row = row_map.get(chunk_id)
            if row is None:
                continue
            results.append(self._fuzzy_row(row, scores.get(chunk_id, 0.0)))
        return results

    def _fuzzy_row(self, row: dict[str, Any], score: float) -> dict[str, Any]:
        result = dict(row)
        result["match_score"] = score
        result["source"] = f"fuzzy {score:.3f}"
        result["snippet"] = fuzzy_snippet(str(result.get("snippet") or ""))
        return result

    def _candidate_count(self, limit: int, offset: int) -> int:
        configured = max(1, int(self.settings.vector_search_candidates or 5000))
        needed = max(200, (max(0, offset) + max(1, limit)) * 20)
        return min(configured, needed)

    def _model_files_ready(self, model_dir: Path) -> bool:
        return all((model_dir / name).exists() for name in MODEL_READY_FILES)

    def _raise_if_cancelled(self, job_id: int | None) -> None:
        if job_id is not None and self.db.job_cancelled(job_id):
            raise VectorJobCancelled("Cancelled by user")


def prompt_query(query: str) -> str:
    detail = f"Instruct: {MODEL_CARD_QUERY_TASK}\nQuery:{str(query or '').strip()}"
    return prompt_document(detail)


def prompt_document(text: str) -> str:
    return f'This sentence: <|im_start|>“{str(text or "").strip()}” means in one word: “'


def fuzzy_snippet(text: str, max_chars: int = 160) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}..."


def remote_error_is_oom(message: str) -> bool:
    value = str(message or "").lower()
    return (
        "out of memory" in value
        or "cuda oom" in value
        or ("cuda" in value and "memory" in value)
    )


def remote_error_is_timeout(message: str) -> bool:
    value = str(message or "").lower()
    return (
        "timeout" in value
        or "timed out" in value
        or "readtimeout" in value
        or "connecttimeout" in value
        or "hard timeout" in value
        or "http 408" in value
        or "http 504" in value
    )


def remote_error_can_split(message: str) -> bool:
    return remote_error_is_oom(message) or remote_error_is_timeout(message)


def remote_status_is_temporarily_unavailable(status_code: int) -> bool:
    return status_code in {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}


@contextmanager
def remote_api_hard_timeout(seconds: float):
    if (
        seconds <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)

    def handle_timeout(_signum, _frame):
        raise EmbeddingAPIHardTimeout(f"embedding API hard timeout after {seconds:.0f}s")

    signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def empty_page(limit: int, offset: int) -> dict[str, Any]:
    return {
        "results": [],
        "count": 0,
        "returned_count": 0,
        "offset": offset,
        "limit": limit,
        "has_more": False,
        "next_offset": None,
    }
