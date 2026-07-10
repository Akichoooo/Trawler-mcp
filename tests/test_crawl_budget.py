from trawler.crawl_budget import CrawlBudget


def test_crawl_budget_stop_reasons():
    budget = CrawlBudget(max_pages=3, max_errors=2, max_links_per_page=5)

    assert budget.stop_reason({"terminal": 2, "error": 1}) == ""
    assert budget.stop_reason({"terminal": 3, "error": 1}) == "page-limit"
    assert budget.stop_reason({"terminal": 1, "error": 2}) == "error-limit"
    assert budget.status_for_stop_reason("page-limit") == "completed"
    assert budget.status_for_stop_reason("error-limit") == "failed"


def test_crawl_budget_from_config(monkeypatch):
    from trawler import config

    monkeypatch.setattr(config, "CRAWL_MAX_ERRORS", 4)
    monkeypatch.setattr(config, "MAX_LINKS_PER_PAGE", 7)

    budget = CrawlBudget.from_config(10)

    assert budget.max_pages == 10
    assert budget.max_errors == 4
    assert budget.max_links_per_page == 7
