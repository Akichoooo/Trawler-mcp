from trawler import db, frontier, jobs
from trawler.crawl_site import cancel_job, get_job_errors, get_job_results, get_job_status


def test_frontier_enqueue_lease_and_mark(tmp_db):
    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com", total=3)
        assert frontier.enqueue(conn, job_id, "https://example.com/a", depth=1)
        assert not frontier.enqueue(conn, job_id, "https://example.com/a", depth=1)

        leased = frontier.lease_next(conn, job_id)
        assert leased is not None
        assert leased["url"] == "https://example.com/a"

        frontier.mark_fetched(conn, job_id, leased["url"], raw_id="raw123")
        counts = frontier.counts(conn, job_id)
        assert counts["fetched"] == 1
        assert counts["terminal"] == 1
    finally:
        conn.close()


def test_frontier_errors_and_result_pagination(tmp_db):
    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com", total=3)
        for idx in range(3):
            url = f"https://example.com/{idx}"
            frontier.enqueue(conn, job_id, url, depth=idx)
            leased = frontier.lease_next(conn, job_id)
            assert leased is not None
            if idx == 1:
                frontier.mark_error(conn, job_id, url, "__TRAWLER_ERROR__:boom")
            else:
                frontier.mark_fetched(conn, job_id, url, raw_id=f"raw{idx}")

        errors = frontier.errors(conn, job_id)
        assert len(errors) == 1
        assert errors[0]["url"] == "https://example.com/1"

        first_page = frontier.result_page(conn, job_id, limit=2)
        assert len(first_page["items"]) == 2
        assert first_page["next_cursor"] == 2
        second_page = frontier.result_page(conn, job_id, cursor=first_page["next_cursor"], limit=2)
        assert len(second_page["items"]) == 1
        assert second_page["next_cursor"] is None
    finally:
        conn.close()


def test_frontier_mark_retry_returns_to_future_queue(tmp_db, monkeypatch):
    import time

    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com", total=1)
        frontier.enqueue(conn, job_id, "https://example.com/a")
        leased = frontier.lease_next(conn, job_id)
        assert leased is not None

        now = time.time()
        retry_count = frontier.mark_retry(
            conn,
            job_id,
            leased["url"],
            "__TRAWLER_ERROR__:{}",
            delay_seconds=10,
        )

        assert retry_count == 1
        assert frontier.lease_next(conn, job_id) is None
        row = conn.execute(
            "SELECT status, retry_count, next_fetch_at FROM frontier_requests WHERE job_id=? AND url=?",
            (job_id, leased["url"]),
        ).fetchone()
        assert row["status"] == "queued"
        assert row["retry_count"] == 1
        assert row["next_fetch_at"] >= now + 9
    finally:
        conn.close()


def test_crawl_site_job_helpers_use_frontier(tmp_db):
    conn = db.connect()
    try:
        job_id = jobs.create_job(conn, "https://example.com", total=1)
        frontier.enqueue(conn, job_id, "https://example.com/a")
        frontier.mark_error(conn, job_id, "https://example.com/a", "nope")
        jobs.update_progress(
            conn,
            job_id,
            visited=["https://example.com/a"],
            queue=[],
            completed=1,
        )
    finally:
        conn.close()

    status = get_job_status(job_id)
    assert status is not None
    assert status["frontier"]["error"] == 1
    assert get_job_errors(job_id)[0]["last_error"] == "nope"
    assert get_job_results(job_id)["items"][0]["status"] == "error"
    assert cancel_job(job_id) is True
