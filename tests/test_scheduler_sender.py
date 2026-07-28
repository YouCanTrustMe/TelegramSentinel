"""Scheduling times (incl. the pre-digest collect-before-classify ordering),
the Bot API retry_after parser, message-id construction and SQL statement split."""
from src.db.base import _split_sql_statements
from src.dispatcher.sender import _retry_after
from src.processor.dedup.deduplicator import make_message_id
from src.scheduler import _pre_classify_time, _pre_collect_time, _summarize_digest_health


def test_digest_health_no_digests_is_dead_mans_switch():
    line, alert = _summarize_digest_health([], 26)
    assert alert is not None and "stuck" in alert
    assert "26h" in line


def test_digest_health_all_ok_no_alert():
    rows = [
        {"sent_at": "2026-07-06 09:00", "items_total": 120, "status": "ok"},
        {"sent_at": "2026-07-06 14:00", "items_total": 67, "status": "ok"},
        {"sent_at": "2026-07-06 19:00", "items_total": 95, "status": "ok"},
    ]
    line, alert = _summarize_digest_health(rows, 26)
    assert alert is None
    assert "3 digest(s)" in line and "282 item(s)" in line and "0 non-ok" in line


def test_digest_health_partial_run_alerts():
    rows = [
        {"sent_at": "2026-07-06 09:00", "items_total": 40, "status": "ok"},
        {"sent_at": "2026-07-06 14:00", "items_total": 10, "status": "partial"},
    ]
    line, alert = _summarize_digest_health(rows, 26)
    assert alert is not None and "partial" in alert and "1/2" in alert
    assert "1 non-ok" in line


def test_digest_health_tolerates_missing_keys():
    # Rows that lack items_total/status (e.g. a sparse Row) must not crash the helper.
    line, alert = _summarize_digest_health([{"sent_at": "x"}], 26)
    assert alert is None
    assert "1 digest(s)" in line and "0 item(s)" in line


def test_pre_digest_ordering_collect_before_classify():
    # The fix: collect runs at T-2 and classify at T-1, so fresh items are
    # summarised before the digest instead of in the slower inline reclassify.
    h, m = 16, 0
    assert _pre_collect_time(h, m) == (15, 58)
    assert _pre_classify_time(h, m) == (15, 59)
    assert _pre_collect_time(h, m) < _pre_classify_time(h, m) < (h, m)


def test_pre_digest_times_wrap_past_midnight():
    assert _pre_collect_time(0, 1) == (23, 59)
    assert _pre_classify_time(0, 0) == (23, 59)


def test_retry_after():
    assert _retry_after('{"parameters": {"retry_after": 7}}') == 7.0
    assert _retry_after('{"ok": true}') == 3.0      # no parameters -> default
    assert _retry_after("not json at all") == 3.0


def test_make_message_id():
    assert make_message_id("telegram", "@chan", "5") == "tg_@chan_5"
    rss_a = make_message_id("rss", "http://feed", "item-1")
    rss_b = make_message_id("rss", "http://feed", "item-1")
    rss_c = make_message_id("rss", "http://feed", "item-2")
    assert rss_a == rss_b and rss_a != rss_c
    assert len(rss_a) == 32  # md5 hex


def test_split_sql_respects_quoted_semicolons():
    sql = "CREATE TABLE t (a TEXT DEFAULT 'x;y'); CREATE INDEX i ON t(a);"
    stmts = [s.strip() for s in _split_sql_statements(sql) if s.strip()]
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE")
