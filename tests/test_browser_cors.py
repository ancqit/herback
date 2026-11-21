from botocore.client import Config

from app.r2 import R2Settings, ensure_browser_cors, load_settings
import app.r2 as r2


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


def test_browser_uploads_are_allowed_from_any_portal_origin(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    calls = []

    class Bucket:
        def put_bucket_cors(self, **kwargs):
            calls.append(kwargs)

    ensure_browser_cors(client=Bucket(), settings=_settings())

    rule = calls[0]["CORSConfiguration"]["CORSRules"][0]
    assert calls[0]["Bucket"] == "sunskriti"
    assert "PUT" in rule["AllowedMethods"]
    assert "https://bin.junction.monster" in rule["AllowedOrigins"]
    assert "https://cart.junction.blog" in rule["AllowedOrigins"]
    assert "https://binter.vercel.app" in rule["AllowedOrigins"]
    assert "content-type" in rule["AllowedHeaders"]
    assert "Content-Type" in rule["AllowedHeaders"]
    assert "*" not in rule["AllowedOrigins"]
    assert "*" not in rule["AllowedHeaders"]


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


def test_bucket_reads_skip_optional_checksums(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "account")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "herb-media")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://files.example")
    monkeypatch.setenv("R2_ENDPOINT", "https://account.r2.cloudflarestorage.com")
    created: dict = {}

    def fake_client(service, **kwargs):
        created.update(kwargs)
        return object()

    monkeypatch.setattr(r2.boto3, "client", fake_client)
    r2._client.cache_clear()
    r2.client_for(load_settings())
    r2._client.cache_clear()

    config = created["config"]
    assert isinstance(config, Config)
    assert config.signature_version == "s3v4"
    assert config.s3["addressing_style"] == "path"
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert created["endpoint_url"] == "https://account.r2.cloudflarestorage.com"
