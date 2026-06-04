"""Azure Blob Storage operations for Parquet files."""

import os
from io import BytesIO

import polars as pl
from azure.storage.blob import BlobServiceClient


def get_blob_client():
    """Returns a BlobServiceClient from AzureWebJobsStorage connection string."""
    conn_str = os.getenv("AzureWebJobsStorage")
    if not conn_str:
        raise ValueError("AzureWebJobsStorage not set in app settings")
    return BlobServiceClient.from_connection_string(conn_str)


def write_parquet(container: str, blob_name: str, df: pl.DataFrame) -> None:
    """Write a Polars DataFrame to Blob Storage as Parquet."""
    client = get_blob_client()
    container_client = client.get_container_client(container)

    buffer = BytesIO()
    df.write_parquet(buffer)
    buffer.seek(0)

    container_client.upload_blob(blob_name, buffer, overwrite=True)


def read_parquet(container: str, blob_name: str) -> pl.DataFrame:
    """Read a Parquet file from Blob Storage as a Polars DataFrame."""
    client = get_blob_client()
    container_client = client.get_container_client(container)

    blob_client = container_client.get_blob_client(blob_name)
    data = blob_client.download_blob().readall()

    return pl.read_parquet(BytesIO(data))


def append_parquet(container: str, blob_name: str, df: pl.DataFrame) -> None:
    """Append rows to an existing Parquet file (read, union, write)."""
    try:
        existing = read_parquet(container, blob_name)
        combined = pl.concat([existing, df], how="diagonal_relaxed")
    except Exception:
        combined = df

    write_parquet(container, blob_name, combined)
