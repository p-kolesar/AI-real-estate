"""Azure Blob Storage I/O for the medallion pipeline.

Single seam between the project's Python code and Blob Storage. Holds the
single-blob Parquet helpers (write/read/append) plus the extensions the
real-estate pipeline needs: listing blobs under a prefix, reading a whole
Hive-partitioned Parquet dataset (bronze), JSON sidecars (the ledger), and a
cheap existence check so read paths degrade gracefully before any data exists.
"""

import json
import os
from io import BytesIO

import polars as pl
from azure.storage.blob import BlobServiceClient


def get_blob_client() -> BlobServiceClient:
    """Returns a BlobServiceClient from the AzureWebJobsStorage connection string."""
    conn_str = os.getenv("AzureWebJobsStorage")
    if not conn_str:
        raise ValueError("AzureWebJobsStorage not set in app settings")
    return BlobServiceClient.from_connection_string(conn_str)


def ensure_container(container: str) -> None:
    """Create the container if it doesn't exist (idempotent). Infra owns it in
    production, but this keeps local Azurite / first-run paths working."""
    client = get_blob_client()
    try:
        client.create_container(container)
    except Exception:
        pass  # already exists (or no permission to create — infra made it)


# ---------------------------------------------------------------------------
# Single-blob Parquet
# ---------------------------------------------------------------------------

def write_parquet(container: str, blob_name: str, df: pl.DataFrame) -> None:
    """Write a Polars DataFrame to Blob Storage as Parquet (overwrites)."""
    client = get_blob_client()
    container_client = client.get_container_client(container)

    buffer = BytesIO()
    df.write_parquet(buffer, compression="zstd")
    buffer.seek(0)

    container_client.upload_blob(blob_name, buffer, overwrite=True)


def read_parquet(container: str, blob_name: str) -> pl.DataFrame:
    """Read a single Parquet blob into a Polars DataFrame."""
    client = get_blob_client()
    container_client = client.get_container_client(container)

    blob_client = container_client.get_blob_client(blob_name)
    data = blob_client.download_blob().readall()

    return pl.read_parquet(BytesIO(data))


def append_parquet(container: str, blob_name: str, df: pl.DataFrame) -> None:
    """Append rows to an existing Parquet blob (read, union, write)."""
    try:
        existing = read_parquet(container, blob_name)
        combined = pl.concat([existing, df], how="diagonal_relaxed")
    except Exception:
        combined = df

    write_parquet(container, blob_name, combined)


# ---------------------------------------------------------------------------
# Extensions: listing, dataset reads, JSON, existence
# ---------------------------------------------------------------------------

def list_blobs(container: str, prefix: str = "") -> list[str]:
    """List blob names under a prefix. Returns [] if the container is missing."""
    client = get_blob_client()
    container_client = client.get_container_client(container)
    try:
        return [b.name for b in container_client.list_blobs(name_starts_with=prefix)]
    except Exception:
        return []


def blob_exists(container: str, blob_name: str) -> bool:
    """True if a blob exists. Used by read paths to degrade gracefully."""
    client = get_blob_client()
    try:
        return client.get_container_client(container).get_blob_client(blob_name).exists()
    except Exception:
        return False


def read_parquet_dataset(container: str, prefix: str) -> pl.DataFrame | None:
    """Read every Parquet blob under `prefix` and concat into one frame.

    Used by the silver rebuild to scan the full Hive-partitioned bronze history.
    Returns None when nothing exists yet (so callers can no-op cleanly). Files
    are unioned with `diagonal_relaxed` so a schema addition in newer slices
    doesn't break older ones.
    """
    names = [n for n in list_blobs(container, prefix) if n.endswith(".parquet")]
    if not names:
        return None
    frames = []
    for name in names:
        try:
            frames.append(read_parquet(container, name))
        except Exception:
            continue  # skip a corrupt/partial slice rather than fail the whole rebuild
    if not frames:
        return None
    return pl.concat(frames, how="diagonal_relaxed")


def write_json(container: str, blob_name: str, obj) -> None:
    """Write a JSON-serializable object to a blob (UTF-8, overwrites)."""
    client = get_blob_client()
    container_client = client.get_container_client(container)
    payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    container_client.upload_blob(blob_name, payload, overwrite=True)


def read_json(container: str, blob_name: str, default=None):
    """Read and parse a JSON blob. Returns `default` if it doesn't exist."""
    if not blob_exists(container, blob_name):
        return default
    client = get_blob_client()
    container_client = client.get_container_client(container)
    data = container_client.get_blob_client(blob_name).download_blob().readall()
    return json.loads(data)
