"""日志脱敏过滤器与文本脱敏函数。"""

from __future__ import annotations

import logging

from dbquery.redaction import RedactingFilter, redact


def test_log_redaction_masks_canary() -> None:
    secret = "canary-password-0001"

    rendered = redact(f"mysql+asyncmy://u:{secret}@host/sales", [secret])

    assert secret not in rendered
    assert "host/sales" in rendered


def test_url_userinfo_masked_without_secret_list() -> None:
    rendered = redact("mysql+asyncmy://u:topsecret@host/sales", [])

    assert "topsecret" not in rendered
    assert "mysql+asyncmy://u:***@host/sales" == rendered


def test_control_characters_escaped_and_long_text_truncated() -> None:
    rendered = redact("a\nb" + "x" * 5000, [])

    assert "\n" not in rendered
    assert len(rendered) < 5200


def test_logging_filter_masks_secret_from_any_logger(caplog) -> None:
    secret = "canary-secret-123"
    logger = logging.getLogger("sqlglot")
    log_filter = RedactingFilter(lambda: [secret])
    logger.addFilter(log_filter)
    try:
        with caplog.at_level(logging.WARNING, logger="sqlglot"):
            logger.warning("parse failed: mysql://u:%s@host/db", secret)
    finally:
        logger.removeFilter(log_filter)

    assert secret not in caplog.text
    assert "mysql://u:***@host/db" in caplog.text


def test_install_redaction_filters_propagated_records() -> None:
    """子 logger 的记录经 root handler 输出时也必须脱敏（接线本身的有效证据）。"""
    import io

    from dbquery.redaction import install_redaction

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    old_handlers, old_filters, old_level = root.handlers[:], root.filters[:], root.level
    root.handlers = [handler]
    root.filters = []
    try:
        install_redaction(lambda: ["canary-secret-123"])
        logging.getLogger("sqlglot").warning("parse url mysql://u:canary-secret-123@host/db")
    finally:
        root.handlers = old_handlers
        root.filters = old_filters
        root.setLevel(old_level)

    output = stream.getvalue()
    assert "canary-secret-123" not in output
    assert "mysql://u:***@host/db" in output
