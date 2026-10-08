#!/usr/bin/env python3
"""
A/B/C experiment for RAGFlow.

What the script does:
1. Uses already-uploaded documents in datasets A/B/C.
2. Applies dataset-level configuration.
3. Applies the same configuration to every existing document.
4. Verifies that the configuration was saved.
5. Starts parsing/indexing.
6. Waits for completion (optional).
7. Prints and saves a summary with source size, chunks, tokens and duration.

Experiment:
A = One, GraphRAG OFF
B = One, GraphRAG ON, community OFF
C = One, GraphRAG ON, community ON

Install:
    pip install requests

Run:
    python ragflow_abc_experiment.py
"""

from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests


# ============================================================
# USER CONFIG
# ============================================================

# No trailing /api/v1 here.
RAGFLOW_URL = "https://ragflow.example.com"

# Example: ragflow-xxxxxxxxxxxxxxxx
API_TOKEN = "ragflow-xxxxxxxxxxxxxxxx"

DATASETS = {
    "A": "dataset_id_a",
    "B": "dataset_id_b",
    "C": "dataset_id_c",
}

# Polling interval while waiting for parsing/indexing.
POLL_INTERVAL_SECONDS = 10

# Documents page size.
PAGE_SIZE = 100

# If True, the script waits until each dataset is fully processed.
WAIT_FOR_COMPLETION = True

# Safer default:
# If documents already have chunks/progress, the script stops instead of
# changing their parser config behind your back.
#
# Set to True only if you intentionally want to work with already parsed docs.
ALLOW_ALREADY_PARSED_DOCUMENTS = False

# File written next to the script with final experiment results.
REPORT_FILE = "ragflow_abc_report.json"


# ============================================================
# EXPERIMENT CONFIGS
# ============================================================

ENTITY_TYPES = [
    "organization",
    "person",
    "event",
    "time",
]

CONFIGS = {
    "A": {
        "chunk_method": "one",
        "parser_config": {
            "chunk_token_num": 1024,
            "graphrag": {
                "use_graphrag": False,
            },
        },
    },

    "B": {
        "chunk_method": "one",
        "parser_config": {
            "chunk_token_num": 1024,
            "graphrag": {
                "use_graphrag": True,
                "entity_types": ENTITY_TYPES,
                "method": "light",
                "community": False,
            },
        },
    },

    "C": {
        "chunk_method": "one",
        "parser_config": {
            "chunk_token_num": 1024,
            "graphrag": {
                "use_graphrag": True,
                "entity_types": ENTITY_TYPES,
                "method": "light",
                "community": True,
            },
        },
    },
}


# ============================================================
# HTTP CLIENT
# ============================================================

session = requests.Session()
session.headers.update(
    {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    }
)


def api_url(endpoint: str) -> str:
    return f"{RAGFLOW_URL.rstrip('/')}/api/v1/{endpoint.lstrip('/')}"


def request_json(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    response = session.request(
        method=method,
        url=api_url(endpoint),
        params=params,
        json=body,
        timeout=timeout,
    )

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            f"{method} {endpoint}: HTTP {response.status_code}, "
            f"non-JSON response:\n{response.text[:2000]}"
        ) from None

    if not response.ok:
        raise RuntimeError(
            f"{method} {endpoint}: HTTP {response.status_code}\n"
            f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
        )

    if payload.get("code", 0) != 0:
        raise RuntimeError(
            f"{method} {endpoint}: RAGFlow error\n"
            f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
        )

    return payload


# ============================================================
# HELPERS
# ============================================================

def human_size(value: int | float) -> str:
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PiB"


def pretty(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def doc_token_count(doc: dict[str, Any]) -> int:
    # Different RAGFlow versions have used different names.
    return int(doc.get("token_count") or doc.get("token_num") or 0)


def doc_chunk_count(doc: dict[str, Any]) -> int:
    return int(doc.get("chunk_count") or doc.get("chunk_num") or 0)


def doc_progress(doc: dict[str, Any]) -> float:
    try:
        return float(doc.get("progress") or 0)
    except (TypeError, ValueError):
        return 0.0


# ============================================================
# DATASET / DOCUMENT API
# ============================================================

def get_documents(dataset_id: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    page = 1

    while True:
        payload = request_json(
            "GET",
            f"datasets/{dataset_id}/documents",
            params={
                "page": page,
                "page_size": PAGE_SIZE,
            },
        )

        data = payload.get("data") or {}
        docs = data.get("docs") or []

        documents.extend(docs)

        total = data.get("total")
        if total is not None and len(documents) >= int(total):
            break

        if len(docs) < PAGE_SIZE:
            break

        page += 1

    return documents


def configure_dataset(dataset_id: str, config: dict[str, Any]) -> None:
    """
    Update default parsing configuration of the dataset.
    """
    payload = request_json(
        "PUT",
        f"datasets/{dataset_id}",
        body=deepcopy(config),
    )

    print("  dataset config updated")
    if payload.get("data") is not None:
        # Avoid flooding the console with huge responses.
        pass


def configure_document(
    dataset_id: str,
    document_id: str,
    config: dict[str, Any],
) -> None:
    """
    Update parser config of an already-uploaded document.
    """
    request_json(
        "PATCH",
        f"datasets/{dataset_id}/documents/{document_id}",
        body=deepcopy(config),
    )


def start_indexing(dataset_id: str, document_ids: list[str]) -> None:
    """
    Start parsing/indexing for the selected documents.
    """
    request_json(
        "POST",
        f"datasets/{dataset_id}/chunks",
        body={"document_ids": document_ids},
    )


# ============================================================
# VALIDATION
# ============================================================

def ensure_safe_to_configure(
    dataset_name: str,
    documents: list[dict[str, Any]],
) -> None:
    already_parsed = []

    for doc in documents:
        chunks = doc_chunk_count(doc)
        progress = doc_progress(doc)

        # progress can differ slightly by version, so chunk count is the
        # strongest simple signal here.
        if chunks > 0 or progress > 0:
            already_parsed.append(
                {
                    "name": doc.get("name"),
                    "id": doc.get("id"),
                    "chunks": chunks,
                    "progress": progress,
                    "run": doc.get("run"),
                }
            )

    if already_parsed and not ALLOW_ALREADY_PARSED_DOCUMENTS:
        print(f"\nDataset {dataset_name} contains already processed documents.")
        print(
            "For a clean A/B/C experiment the datasets should contain "
            "uploaded but not yet parsed documents."
        )
        print("\nFirst examples:")
        print(pretty(already_parsed[:10]))
        print(
            "\nScript stopped for safety. "
            "If this is intentional, set "
            "ALLOW_ALREADY_PARSED_DOCUMENTS = True."
        )
        sys.exit(2)


def expected_graphrag(dataset_name: str) -> dict[str, Any]:
    return CONFIGS[dataset_name]["parser_config"]["graphrag"]


def verify_documents(
    dataset_name: str,
    documents: list[dict[str, Any]],
) -> None:
    expected_method = CONFIGS[dataset_name]["chunk_method"]
    expected_graph = expected_graphrag(dataset_name)

    errors = []

    for doc in documents:
        parser_config = doc.get("parser_config") or {}
        graph = parser_config.get("graphrag") or {}

        if doc.get("chunk_method") != expected_method:
            errors.append(
                f"{doc.get('name')}: chunk_method="
                f"{doc.get('chunk_method')!r}, expected {expected_method!r}"
            )
            continue

        # Compare only the keys we explicitly set. RAGFlow can add defaults.
        for key, expected_value in expected_graph.items():
            if graph.get(key) != expected_value:
                errors.append(
                    f"{doc.get('name')}: graphrag.{key}="
                    f"{graph.get(key)!r}, expected {expected_value!r}"
                )

    if errors:
        print(f"\nVerification failed for dataset {dataset_name}:")
        for error in errors[:20]:
            print("  -", error)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        raise RuntimeError(
            f"Document configuration verification failed for {dataset_name}"
        )

    print(f"  verified {len(documents)} documents")


# ============================================================
# MONITORING / REPORT
# ============================================================

def print_progress(dataset_name: str, documents: list[dict[str, Any]]) -> None:
    total = len(documents)
    finished = sum(1 for d in documents if doc_progress(d) >= 1.0)
    chunks = sum(doc_chunk_count(d) for d in documents)
    tokens = sum(doc_token_count(d) for d in documents)

    progress_values = [doc_progress(d) for d in documents]
    average_progress = (
        sum(progress_values) / len(progress_values)
        if progress_values
        else 0
    )

    print(
        f"[{dataset_name}] "
        f"finished={finished}/{total}, "
        f"avg_progress={average_progress:.1%}, "
        f"chunks={chunks:,}, "
        f"tokens={tokens:,}"
    )


def document_failed(doc: dict[str, Any]) -> bool:
    """
    RAGFlow run-state representation has changed between versions.
    We therefore use progress_msg as an additional generic failure signal.
    """
    msg = str(doc.get("progress_msg") or "").lower()
    failure_words = ("error", "failed", "exception", "traceback")
    return any(word in msg for word in failure_words)


def wait_until_finished(
    dataset_name: str,
    dataset_id: str,
) -> list[dict[str, Any]]:
    while True:
        documents = get_documents(dataset_id)
        print_progress(dataset_name, documents)

        failed = [d for d in documents if document_failed(d)]
        if failed:
            print(f"\nErrors detected in dataset {dataset_name}:")
            for doc in failed[:10]:
                print(
                    f"- {doc.get('name')}: "
                    f"{doc.get('progress_msg')}"
                )
            raise RuntimeError(
                f"Parsing/indexing failed in dataset {dataset_name}"
            )

        if documents and all(doc_progress(d) >= 1.0 for d in documents):
            return documents

        time.sleep(POLL_INTERVAL_SECONDS)


def build_dataset_stats(
    dataset_name: str,
    dataset_id: str,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    source_size = sum(int(d.get("size") or 0) for d in documents)
    chunks = sum(doc_chunk_count(d) for d in documents)
    tokens = sum(doc_token_count(d) for d in documents)

    durations = []
    for d in documents:
        value = d.get("process_duration")
        if value is not None:
            try:
                durations.append(float(value))
            except (TypeError, ValueError):
                pass

    return {
        "dataset": dataset_name,
        "dataset_id": dataset_id,
        "documents": len(documents),
        "source_size_bytes": source_size,
        "source_size_human": human_size(source_size),
        "chunks": chunks,
        "tokens": tokens,
        "chunks_per_document": (
            chunks / len(documents) if documents else 0
        ),
        "tokens_per_document": (
            tokens / len(documents) if documents else 0
        ),
        "tokens_per_chunk": (
            tokens / chunks if chunks else 0
        ),
        "sum_process_duration": (
            sum(durations) if durations else None
        ),
        "config": deepcopy(CONFIGS[dataset_name]),
    }


def print_final_table(report: dict[str, dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print("FINAL A/B/C REPORT")
    print("=" * 100)

    header = (
        f"{'Dataset':<8}"
        f"{'Docs':>10}"
        f"{'Source':>14}"
        f"{'Chunks':>14}"
        f"{'Tokens':>18}"
        f"{'Chunks/doc':>14}"
        f"{'Tokens/chunk':>16}"
    )
    print(header)
    print("-" * len(header))

    for name in ("A", "B", "C"):
        stat = report[name]
        print(
            f"{name:<8}"
            f"{stat['documents']:>10,}"
            f"{stat['source_size_human']:>14}"
            f"{stat['chunks']:>14,}"
            f"{stat['tokens']:>18,}"
            f"{stat['chunks_per_document']:>14.2f}"
            f"{stat['tokens_per_chunk']:>16,.0f}"
        )

    print("\nExperiment:")
    print("  A = One, GraphRAG OFF")
    print("  B = One, GraphRAG ON, community OFF")
    print("  C = One, GraphRAG ON, community ON")
    print()
    print(
        "Important: these API metrics are logical RAGFlow metrics. "
        "They are NOT the physical Elasticsearch/Infinity/OpenSearch "
        "store size on disk."
    )


# ============================================================
# MAIN
# ============================================================

def validate_user_config() -> None:
    placeholders = []

    if "ragflow.example.com" in RAGFLOW_URL:
        placeholders.append("RAGFLOW_URL")

    if API_TOKEN == "ragflow-xxxxxxxxxxxxxxxx":
        placeholders.append("API_TOKEN")

    for name, dataset_id in DATASETS.items():
        if dataset_id == f"dataset_id_{name.lower()}":
            placeholders.append(f"DATASETS['{name}']")

    if placeholders:
        raise RuntimeError(
            "Fill these values at the top of the script first: "
            + ", ".join(placeholders)
        )


def main() -> None:
    validate_user_config()

    print("=" * 100)
    print("RAGFLOW A/B/C EXPERIMENT")
    print("=" * 100)
    print(f"RAGFlow: {RAGFLOW_URL}")
    print()

    # --------------------------------------------------------
    # 1. Load docs and safety-check BEFORE changing anything.
    # --------------------------------------------------------

    initial_docs: dict[str, list[dict[str, Any]]] = {}

    print("STEP 1: Loading existing documents")
    for name, dataset_id in DATASETS.items():
        docs = get_documents(dataset_id)
        initial_docs[name] = docs

        source_size = sum(int(d.get("size") or 0) for d in docs)

        print(
            f"  {name}: {len(docs):,} docs, "
            f"source={human_size(source_size)}"
        )

        if not docs:
            raise RuntimeError(
                f"Dataset {name} ({dataset_id}) has no documents"
            )

        ensure_safe_to_configure(name, docs)

    # For a clean experiment the same input should exist in A/B/C.
    counts = {name: len(docs) for name, docs in initial_docs.items()}
    if len(set(counts.values())) != 1:
        print(
            "\nWARNING: A/B/C contain different numbers of documents:"
        )
        print(pretty(counts))

    # --------------------------------------------------------
    # 2. Configure dataset defaults.
    # --------------------------------------------------------

    print("\nSTEP 2: Updating dataset-level configs")
    for name, dataset_id in DATASETS.items():
        print(f"[{name}]")
        configure_dataset(dataset_id, CONFIGS[name])

    # --------------------------------------------------------
    # 3. Configure every already-uploaded document.
    # --------------------------------------------------------

    print("\nSTEP 3: Updating already-uploaded documents")
    for name, dataset_id in DATASETS.items():
        docs = initial_docs[name]
        config = CONFIGS[name]

        print(f"[{name}] {len(docs):,} documents")

        for index, doc in enumerate(docs, start=1):
            configure_document(
                dataset_id=dataset_id,
                document_id=doc["id"],
                config=config,
            )

            if (
                index == 1
                or index % 25 == 0
                or index == len(docs)
            ):
                print(f"  configured {index:,}/{len(docs):,}")

    # --------------------------------------------------------
    # 4. Reload and verify.
    # --------------------------------------------------------

    print("\nSTEP 4: Verifying saved configs")
    verified_docs: dict[str, list[dict[str, Any]]] = {}

    for name, dataset_id in DATASETS.items():
        docs = get_documents(dataset_id)
        verify_documents(name, docs)
        verified_docs[name] = docs

        sample = docs[0]
        print(
            f"  {name} sample {sample.get('name')!r}: "
            f"chunk_method={sample.get('chunk_method')}, "
            f"graphrag="
            f"{pretty((sample.get('parser_config') or {}).get('graphrag') or {})}"
        )

    # --------------------------------------------------------
    # 5. Start indexing.
    #
    # We start A -> wait, then B -> wait, then C -> wait.
    # This avoids A/B/C competing for the same server resources,
    # which gives more interpretable processing-time results.
    # --------------------------------------------------------

    print("\nSTEP 5: Starting parsing/indexing")

    final_docs: dict[str, list[dict[str, Any]]] = {}

    for name, dataset_id in DATASETS.items():
        docs = verified_docs[name]
        document_ids = [doc["id"] for doc in docs]

        print(
            f"\n[{name}] starting {len(document_ids):,} documents..."
        )

        start_indexing(dataset_id, document_ids)
        print(f"[{name}] indexing request accepted")

        if WAIT_FOR_COMPLETION:
            print(f"[{name}] waiting for completion...")
            final_docs[name] = wait_until_finished(
                dataset_name=name,
                dataset_id=dataset_id,
            )
            print(f"[{name}] finished")
        else:
            final_docs[name] = get_documents(dataset_id)

    # --------------------------------------------------------
    # 6. Build report.
    # --------------------------------------------------------

    print("\nSTEP 6: Building report")

    report = {
        name: build_dataset_stats(
            dataset_name=name,
            dataset_id=DATASETS[name],
            documents=final_docs[name],
        )
        for name in ("A", "B", "C")
    }

    print_final_table(report)

    report_path = Path(__file__).resolve().parent / REPORT_FILE
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nJSON report saved to: {report_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
