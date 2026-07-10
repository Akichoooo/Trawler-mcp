from trawler import browser_session, db


def test_select_session_is_stable_and_tracks_use(tmp_db):
    conn = db.connect()
    try:
        first = browser_session.select_session(
            conn,
            "example.com",
            proxy_url="http://proxy.local:8080",
            storage_state_bound=True,
        )
        second = browser_session.select_session(
            conn,
            "example.com",
            proxy_url="http://proxy.local:8080",
            storage_state_bound=True,
        )

        assert first.session_id == second.session_id
        loaded = browser_session.get_session(conn, first.session_id)
        assert loaded is not None
        assert loaded.use_count == 2
        assert loaded.fingerprint_key == first.session_id
    finally:
        conn.close()


def test_session_health_and_retirement(tmp_db):
    conn = db.connect()
    try:
        session = browser_session.select_session(conn, "example.com")
        browser_session.mark_bad(conn, session.session_id, "blocked-bot")
        after_bad = browser_session.get_session(conn, session.session_id)
        assert after_bad is not None
        assert after_bad.error_score == 1
        assert after_bad.last_error == "blocked-bot"

        browser_session.mark_good(conn, session.session_id)
        after_good = browser_session.get_session(conn, session.session_id)
        assert after_good is not None
        assert after_good.success_count == 1
        assert after_good.error_score == 0

        browser_session.retire_session(conn, session.session_id, "session-expired")
        retired = browser_session.get_session(conn, session.session_id)
        assert retired is not None
        assert retired.status == "retired"
        assert retired.retired_at
    finally:
        conn.close()


def test_retired_session_is_not_reactivated(tmp_db):
    conn = db.connect()
    try:
        first = browser_session.select_session(conn, "example.com")
        browser_session.retire_session(conn, first.session_id, "blocked-bot")

        second = browser_session.select_session(conn, "example.com")

        assert second.session_id != first.session_id
        assert second.status == "active"
        assert second.fingerprint_key == second.session_id
        retired = browser_session.get_session(conn, first.session_id)
        assert retired is not None
        assert retired.status == "retired"
    finally:
        conn.close()
