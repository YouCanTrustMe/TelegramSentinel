"""Scheduling times (incl. the pre-digest collect-before-classify ordering),
the Bot API retry_after parser, message-id construction and SQL statement split."""
from src.db.base import _split_sql_statements
from src.dispatcher.sender import _retry_after
from src.processor.dedup.deduplicator import make_message_id
import src.scheduler as scheduler
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


def test_new_silent_sources_pushes_each_source_once():
    rows = [{"id": 79, "name": "MarketWatch"}, {"id": 82, "name": "Import AI"}]

    fresh, state = scheduler._new_silent_sources(rows, set())
    assert [r["id"] for r in fresh] == [79, 82]
    assert state == {79, 82}

    fresh, state = scheduler._new_silent_sources(rows, state)
    assert fresh == []            # same two next morning: no second push
    assert state == {79, 82}


def test_a_source_that_recovers_can_alert_again():
    """The memo holds only sources that are silent RIGHT NOW, so one that starts
    publishing again drops out and is free to raise the alarm if it dies later."""
    fresh, state = scheduler._new_silent_sources([{"id": 79, "name": "MarketWatch"}], {79, 82})

    assert fresh == []
    assert state == {79}          # 82 recovered -> forgotten

    fresh, _ = scheduler._new_silent_sources(
        [{"id": 79, "name": "MarketWatch"}, {"id": 82, "name": "Import AI"}], state)
    assert [r["id"] for r in fresh] == [82]


def test_silent_source_line_reports_a_source_that_never_published():
    line = scheduler._silent_source_line(
        {"id": 22, "name": "Zagreb up to you", "type": "telegram",
         "last_item_at": None, "hours_silent": None})

    assert "never" in line and "Zagreb up to you" in line


def test_a_source_added_today_is_not_reported_as_silent():
    """A source that has never produced is only suspicious once it has had the whole
    window to produce — otherwise every new source alerts the morning after."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 4, 5, 15, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "name": "Added today", "last_item_at": None,
         "created_at": (now - timedelta(hours=6)).isoformat()},
        {"id": 2, "name": "Never produced, added in July", "last_item_at": None,
         "created_at": (now - timedelta(days=60)).isoformat()},
    ]

    fresh, state = scheduler._new_silent_sources(rows, set(), now=now)

    assert [r["id"] for r in fresh] == [2]
    assert state == {2}          # the young one is not memoised either, so it can alert later
