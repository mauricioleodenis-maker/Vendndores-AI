import json

from app.core.logging import configure_logging, get_logger, redact_text


def test_redact_text():
    assert "[email]" in redact_text("escribe a juan@correo.com ya")
    assert "[tel]" in redact_text("llama al +57 300 111 2233")
    assert "sk-ant" not in redact_text("clave sk-ant-api03-abcdefghijk")
    assert "abc12345678" not in redact_text("Authorization: Bearer abc12345678xyz")


def test_logger_redacts_sensitive_keys_and_values(capsys):
    configure_logging("INFO", json_logs=True)
    get_logger("t").info("evento", password="hunter2", phone="+573001112233", note="mail a a@b.co")
    out = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(out)
    assert data["password"] == "[redacted]" and data["phone"] == "[redacted]"
    assert data["note"] == "mail a [email]"
    assert "hunter2" not in out
