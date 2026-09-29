import hashlib
import hmac

from app.main import verify_signature


def test_verify_signature_accepts_valid():
    secret = "s3cret"
    body = b'{"action":"opened"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig)


def test_verify_signature_rejects_bad():
    assert not verify_signature("s3cret", b"{}", "sha256=deadbeef")
    assert not verify_signature("s3cret", b"{}", None)


def test_verify_signature_open_when_no_secret():
    assert verify_signature("", b"{}", None)
