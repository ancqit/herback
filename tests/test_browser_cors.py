from app.r2 import R2Settings, ensure_browser_cors


def _settings() -> R2Settings:
    return R2Settings(
        account_id="account",
        access_key_id="key",
        secret_access_key="secret",
        bucket="sunskriti",
        public_base_url="https://files.example",
        endpoint="https://account.r2.cloudflarestorage.com",
        sign_expires_seconds=600,
        cache_control="public, max-age=31536000, immutable",
    )


def test_browser_uploads_are_allowed_from_any_portal_origin():
    calls = []

    class Bucket:
        def put_bucket_cors(self, **kwargs):
            calls.append(kwargs)

    ensure_browser_cors(client=Bucket(), settings=_settings())

    rule = calls[0]["CORSConfiguration"]["CORSRules"][0]
    assert calls[0]["Bucket"] == "sunskriti"
    assert "PUT" in rule["AllowedMethods"]
    assert "*" in rule["AllowedOrigins"]
    assert "*" in rule["AllowedHeaders"]


def test_missing_r2_skips_the_bucket_rule():
    class Bucket:
        def put_bucket_cors(self, **kwargs):
            raise AssertionError("the bucket should stay untouched")

    blank = R2Settings(
        account_id="",
        access_key_id="",
        secret_access_key="",
        bucket="",
        public_base_url="",
        endpoint="",
        sign_expires_seconds=600,
        cache_control="",
    )
    ensure_browser_cors(client=Bucket(), settings=blank)
