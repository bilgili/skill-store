"""Run against an explicitly configured disposable MinIO server."""

import os
import uuid

import boto3
import pytest
from botocore.config import Config

from skill_store.backends import S3Backend
from skill_store.models import StoreConfig


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("SKILL_STORE_MINIO_ENDPOINT"), reason="MinIO endpoint is not configured"
)
def test_minio_endpoint_replace_orphans_and_point_verification():
    endpoint = os.environ["SKILL_STORE_MINIO_ENDPOINT"]
    access_key = os.environ["SKILL_STORE_MINIO_ACCESS_KEY"]
    secret_key = os.environ["SKILL_STORE_MINIO_SECRET_KEY"]
    bucket = "skill-store-test-" + uuid.uuid4().hex
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2}),
    )
    client.create_bucket(Bucket=bucket)
    try:
        backend = S3Backend(
            StoreConfig(
                "minio",
                "s3",
                endpoint=endpoint,
                bucket=bucket,
                prefix="library",
                access_key=access_key,
                secret_key=secret_key,
            )
        )
        original = {"SKILL.md": b"# Real MinIO", "old.txt": b"retired", "nested/raw": b"\x00\xff"}
        backend.write_tree("sample", original)
        backend.verify_tree("sample", original)
        assert backend.list_skills() == {"sample": original}
        replacement = {"SKILL.md": b"# Updated", "nested/new": b"new"}
        backend.write_tree("sample", replacement, replace=True)
        backend.verify_tree("sample", replacement)
        assert backend.list_skills() == {"sample": replacement}
        keys = client.list_objects_v2(Bucket=bucket)["Contents"]
        assert {entry["Key"] for entry in keys} == {
            "library/sample/SKILL.md",
            "library/sample/nested/new",
        }
        backend.delete_skill("sample")
        assert backend.list_skills() == {}
    finally:
        for entry in client.list_objects_v2(Bucket=bucket).get("Contents", []):
            client.delete_object(Bucket=bucket, Key=entry["Key"])
        client.delete_bucket(Bucket=bucket)
