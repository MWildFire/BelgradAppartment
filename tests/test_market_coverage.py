from datetime import UTC, datetime, timedelta

from src.jobs.market_coverage import market_coverage


def test_market_coverage_uses_same_full_run_and_marks_partial():
    now = datetime(2026, 9, 28, 6, tzinfo=UTC)
    full = {
        "kind": "full", "finished_at": (now - timedelta(hours=4)).isoformat(),
        "sources": [
            {"platform": "NEKRETNINE", "processed": 3510, "site_total": 3513,
             "denominator_trusted": True, "crawl_complete": False},
            {"platform": "CETIRI_ZIDA", "processed": 3676, "site_total": 3676,
             "denominator_trusted": True, "crawl_complete": True},
            {"platform": "HALOOGLASI", "processed": 2716, "site_total": 2792,
             "denominator_trusted": True, "crawl_complete": False},
        ],
    }
    incremental = {
        "kind": "incremental", "finished_at": (now - timedelta(hours=1)).isoformat(),
        "sources": [{"platform": "NEKRETNINE", "processed": 50, "site_total": 3513,
                     "denominator_trusted": True, "crawl_complete": False}],
    }
    rows = market_coverage([incremental, full], now=now)
    assert [(r["platform"], r["in_database"], r["site_total"], r["missing"], r["percent"], r["status"])
            for r in rows] == [
        ("NEKRETNINE", 3510, 3513, 3, 99.9, "partial"),
        ("CETIRI_ZIDA", 3676, 3676, 0, 100.0, "complete"),
        ("HALOOGLASI", 2716, 2792, 76, 97.3, "partial"),
    ]


def test_market_coverage_refuses_stale_and_untrusted_denominators():
    now = datetime(2026, 9, 28, 6, tzinfo=UTC)
    stale = {"kind": "full", "finished_at": (now - timedelta(hours=31)).isoformat(),
             "sources": [{"platform": "NEKRETNINE", "processed": 10,
                          "site_total": 20, "denominator_trusted": True}]}
    untrusted = {"kind": "full", "finished_at": (now - timedelta(hours=1)).isoformat(),
                 "sources": [{"platform": "CETIRI_ZIDA", "processed": 15,
                              "site_total": 10, "denominator_trusted": True},
                             {"platform": "HALOOGLASI", "processed": 8,
                              "site_total": 10, "denominator_trusted": False}]}
    rows = market_coverage([stale, untrusted], now=now)
    assert [r["status"] for r in rows] == ["dated", "unverified", "unverified"]
    assert all(r["percent"] is None for r in rows)
