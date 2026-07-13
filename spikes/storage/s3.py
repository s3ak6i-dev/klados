"""Minimal S3 client (AWS SigV4, path-style) — stdlib only, no boto3.

Enough for the object-storage tier: make_bucket / put / get / head against any S3-compatible
endpoint (MinIO here). Path-style addressing (http://host/bucket/key), which MinIO uses.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
from datetime import datetime, timezone

_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class S3:
    def __init__(self, endpoint: str, access: str, secret: str, region: str = "us-east-1"):
        self.host = endpoint  # e.g. "127.0.0.1:9000"
        self.access = access
        self.secret = secret
        self.region = region
        self.service = "s3"

    def _sign_headers(self, method: str, uri: str, payload: bytes) -> dict:
        now = datetime.now(timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest() if payload else _EMPTY_SHA
        canonical_headers = (f"host:{self.host}\n"
                             f"x-amz-content-sha256:{payload_hash}\n"
                             f"x-amz-date:{amzdate}\n")
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = f"{method}\n{uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{datestamp}/{self.region}/{self.service}/aws4_request"
        string_to_sign = (f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n"
                          f"{hashlib.sha256(canonical_request.encode()).hexdigest()}")

        def _hmac(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()

        k_date = _hmac(("AWS4" + self.secret).encode(), datestamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, self.service)
        k_signing = _hmac(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        auth = (f"AWS4-HMAC-SHA256 Credential={self.access}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}")
        return {"Authorization": auth, "x-amz-content-sha256": payload_hash,
                "x-amz-date": amzdate, "Host": self.host}

    def _req(self, method: str, uri: str, payload: bytes = b""):
        headers = self._sign_headers(method, uri, payload)
        conn = http.client.HTTPConnection(self.host, timeout=30)
        conn.request(method, uri, body=payload if payload else None, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def make_bucket(self, bucket: str) -> bool:
        status, _ = self._req("PUT", f"/{bucket}")
        return status in (200, 204, 409)  # 409 = already exists

    def put(self, bucket: str, key: str, data: bytes) -> None:
        status, body = self._req("PUT", f"/{bucket}/{key}", data)
        if status not in (200, 204):
            raise RuntimeError(f"S3 PUT {key} -> {status}: {body[:200]!r}")

    def get(self, bucket: str, key: str) -> bytes:
        status, body = self._req("GET", f"/{bucket}/{key}")
        if status != 200:
            raise RuntimeError(f"S3 GET {key} -> {status}")
        return body

    def head(self, bucket: str, key: str) -> bool:
        status, _ = self._req("HEAD", f"/{bucket}/{key}")
        return status == 200
