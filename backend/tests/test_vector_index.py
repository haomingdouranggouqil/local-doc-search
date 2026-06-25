from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import faiss
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.database import Database
from app.vector_search import (
    VectorSearchService,
    remote_error_can_split,
    remote_status_is_temporarily_unavailable,
)


class VectorIndexTests(unittest.TestCase):
    def test_remote_timeout_errors_can_split_batches(self) -> None:
        self.assertTrue(remote_error_can_split("embedding API hard timeout after 50s"))
        self.assertTrue(remote_error_can_split("ReadTimeout"))
        self.assertTrue(remote_error_can_split("CUDA out of memory"))
        self.assertFalse(remote_error_can_split("HTTP 401 unauthorized"))
        self.assertTrue(remote_status_is_temporarily_unavailable(530))
        self.assertFalse(remote_status_is_temporarily_unavailable(401))

    def test_siliconflow_rate_limit_is_capped_below_l0_rpm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = VectorSearchService(
                Settings(
                    document_root=root,
                    state_dir=root / ".state",
                    siliconflow_requests_per_second=100,
                    resource_auto_tune=False,
                ),
                Database(root / "index.sqlite"),
            )

            self.assertLessEqual(service.status()["request_rate_limit_per_second"], 2000 / 60)

    def test_siliconflow_batches_pack_multiple_lines_without_exceeding_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = VectorSearchService(
                Settings(
                    document_root=root,
                    state_dir=root / ".state",
                    siliconflow_embedding_batch_size=3,
                    siliconflow_max_request_tokens=18,
                    resource_auto_tune=False,
                ),
                Database(root / "index.sqlite"),
            )

            batches = service._pack_embedding_batches(["aaaa", "bbbb", "cccc", "dddddddddddd"])

            self.assertEqual([["aaaa", "bbbb"], ["cccc"], ["dddddddddddd"]], batches)
            for batch in batches:
                self.assertLessEqual(len(batch), 3)
                self.assertLessEqual(service._estimate_batch_tokens(batch), service._max_request_tokens())

    def test_siliconflow_zero_batch_size_packs_by_tokens_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = VectorSearchService(
                Settings(
                    document_root=root,
                    state_dir=root / ".state",
                    siliconflow_embedding_batch_size=0,
                    siliconflow_max_request_tokens=100,
                    resource_auto_tune=False,
                ),
                Database(root / "index.sqlite"),
            )

            inputs = [f"line-{index}" for index in range(10)]
            batches = service._pack_embedding_batches(inputs)

            self.assertEqual([inputs], batches)

    def test_embedding_row_batches_keep_rows_aligned_to_token_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = VectorSearchService(
                Settings(
                    document_root=root,
                    state_dir=root / ".state",
                    siliconflow_embedding_batch_size=0,
                    siliconflow_max_request_tokens=18,
                    resource_auto_tune=False,
                ),
                Database(root / "index.sqlite"),
            )

            rows = [{"id": index, "text": text} for index, text in enumerate(["aaaa", "bbbb", "cccc"])]
            batches = service._pack_embedding_row_batches(rows)

            self.assertEqual([[0, 1], [2]], [[row["id"] for row in batch] for batch in batches])

    def test_document_windows_are_split_by_token_budget_for_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = VectorSearchService(
                Settings(
                    document_root=root,
                    state_dir=root / ".state",
                    siliconflow_embedding_batch_size=0,
                    siliconflow_embedding_concurrency=2,
                    siliconflow_max_request_tokens=20,
                    resource_auto_tune=False,
                ),
                Database(root / "index.sqlite"),
            )

            chunks = [{"id": index, "text": "x" * 16} for index in range(5)]
            windows = service._document_chunk_windows(chunks)

            self.assertEqual([2, 2, 1], [len(window) for window in windows])
            for window in windows:
                self.assertLessEqual(
                    service._estimate_batch_tokens([row["text"] for row in window]),
                    service._document_batch_token_budget(),
                )

    def test_document_window_can_fill_one_minute_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = VectorSearchService(
                Settings(
                    document_root=root,
                    state_dir=root / ".state",
                    siliconflow_embedding_batch_size=0,
                    siliconflow_embedding_concurrency=120,
                    siliconflow_max_request_tokens=30000,
                    siliconflow_tokens_per_minute=1000000,
                    resource_auto_tune=False,
                ),
                Database(root / "index.sqlite"),
            )

            chunks = [{"id": index, "text": "x" * 96} for index in range(12000)]
            windows = service._document_chunk_windows(chunks)

            self.assertGreaterEqual(len(windows[0]), 9000)
            self.assertLessEqual(
                service._estimate_batch_tokens([row["text"] for row in windows[0]]),
                1000000,
            )

    def test_chunks_missing_embeddings_skips_existing_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.txt"),
                    "rel_path": "sample.txt",
                    "title": "sample.txt",
                    "ext": ".txt",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [
                    {"ordinal": 0, "line": 1, "text": "first line", "source": "text"},
                    {"ordinal": 1, "line": 2, "text": "second line", "source": "text"},
                ],
                status="ready",
                searchable_pdf=None,
                page_count=0,
                text_chars=22,
                has_text_layer=True,
            )
            first_chunk = db.chunks_for_embedding(document_id)[0]
            db.upsert_chunk_embeddings(
                document_id,
                "model@512",
                512,
                [(first_chunk["id"], np.zeros(512, dtype="<f4").tobytes())],
            )

            missing = db.chunks_missing_embeddings(document_id, "model@512", 512)

            self.assertEqual(["second line"], [row["text"] for row in missing])

    def test_rebuild_faiss_uses_chunk_ids_and_four_bit_quantization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.txt"),
                    "rel_path": "sample.txt",
                    "title": "sample.txt",
                    "ext": ".txt",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [
                    {"ordinal": 0, "line": 1, "text": "first line", "source": "text"},
                    {"ordinal": 1, "line": 2, "text": "second line", "source": "text"},
                ],
                status="ready",
                searchable_pdf=None,
                page_count=0,
                text_chars=22,
                has_text_layer=True,
            )

            settings = Settings(
                document_root=root,
                state_dir=root / ".state",
                embedding_model_dir=root / "model",
                resource_auto_tune=False,
            )
            settings.ensure_dirs()
            service = VectorSearchService(settings, db)
            chunks = db.chunks_for_embedding(document_id)
            vectors = np.zeros((2, service.dimension), dtype="float32")
            vectors[0, 0] = 1
            vectors[1, 1] = 1
            db.upsert_chunk_embeddings(
                document_id,
                service.model_key,
                service.dimension,
                [
                    (chunks[0]["id"], vectors[0].tobytes()),
                    (chunks[1]["id"], vectors[1].tobytes()),
                ],
            )

            service.rebuild_faiss()

            stats = db.vector_stats(service.model_key, service.dimension)
            self.assertEqual(2, stats["index_count"])
            self.assertEqual(1, stats["index_document_count"])
            self.assertEqual("scalar-4bit", stats["index_type"])
            self.assertFalse(stats["index_dirty"])

            index = faiss.read_index(str(service.index_path))
            scores, ids = index.search(vectors[:1], 1)
            self.assertEqual(chunks[0]["id"], int(ids[0][0]))
            self.assertGreater(float(scores[0][0]), 0.5)

    def test_vector_jobs_do_not_change_document_index_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.txt"),
                    "rel_path": "sample.txt",
                    "title": "sample.txt",
                    "ext": ".txt",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [{"ordinal": 0, "line": 1, "text": "first line", "source": "text"}],
                status="ready",
                searchable_pdf=None,
                page_count=0,
                text_chars=10,
                has_text_layer=True,
            )

            queued = db.enqueue_vector_jobs("model@512", 512, force=True)

            self.assertEqual(1, queued)
            self.assertEqual("ready", db.get_document(document_id)["status"])
            job = db.claim_next_job()
            self.assertEqual("vector", job["type"])
            self.assertEqual("ready", db.get_document(document_id)["status"])

    def test_cancel_vector_jobs_cancels_active_vector_work_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            for index in range(2):
                document_id = f"doc{index}"
                db.upsert_document(
                    {
                        "id": document_id,
                        "path": str(root / f"sample-{index}.txt"),
                        "rel_path": f"sample-{index}.txt",
                        "title": f"sample-{index}.txt",
                        "ext": ".txt",
                        "size": 1,
                        "mtime": 1.0,
                        "sha256": f"hash-{index}",
                        "status": "queued",
                    }
                )
                db.replace_chunks(
                    document_id,
                    [{"ordinal": 0, "line": 1, "text": "first line", "source": "text"}],
                    status="ready",
                    searchable_pdf=None,
                    page_count=0,
                    text_chars=10,
                    has_text_layer=True,
                )

            queued = db.enqueue_vector_jobs("model@512", 512, force=True)
            self.assertEqual(2, queued)
            claimed = db.claim_next_job()
            self.assertEqual("vector", claimed["type"])

            cancelled = db.cancel_vector_jobs()

            self.assertEqual(2, cancelled)
            stats = db.vector_stats("model@512", 512)
            self.assertEqual(0, stats["queued"])
            self.assertEqual(0, stats["processing"])
            self.assertEqual("ready", db.get_document("doc0")["status"])
            self.assertEqual("ready", db.get_document("doc1")["status"])
            with db.connect() as con:
                statuses = [
                    row["status"]
                    for row in con.execute(
                        "SELECT status FROM jobs WHERE type = 'vector' ORDER BY id"
                    )
                ]
            self.assertEqual(["cancelled", "cancelled"], statuses)

    def test_pause_vector_jobs_requeues_active_vector_work_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "index.sqlite")
            document_id = "doc1"
            db.upsert_document(
                {
                    "id": document_id,
                    "path": str(root / "sample.txt"),
                    "rel_path": "sample.txt",
                    "title": "sample.txt",
                    "ext": ".txt",
                    "size": 1,
                    "mtime": 1.0,
                    "sha256": "hash",
                    "status": "queued",
                }
            )
            db.replace_chunks(
                document_id,
                [{"ordinal": 0, "line": 1, "text": "first line", "source": "text"}],
                status="ready",
                searchable_pdf=None,
                page_count=0,
                text_chars=10,
                has_text_layer=True,
            )
            db.enqueue_vector_jobs("model@512", 512, force=True)
            claimed = db.claim_next_job()
            self.assertEqual("vector", claimed["type"])

            paused = db.pause_vector_jobs("remote unavailable")

            self.assertEqual(1, paused)
            stats = db.vector_stats("model@512", 512)
            self.assertEqual(1, stats["queued"])
            self.assertEqual(0, stats["processing"])
            self.assertEqual(0, stats["failed"])
            self.assertEqual("ready", db.get_document(document_id)["status"])


if __name__ == "__main__":
    unittest.main()
