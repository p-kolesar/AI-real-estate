"""Real-estate scraper entry point.

nehnutelnosti.sk result pages render listings client-side behind an F5 ASM
challenge, so plain HTTP yields zero listings — the only approach that returns
data is a real browser rendering the page (see RECON_FINDINGS.md). This module is
therefore a thin delegator to ``realestate.browser_scraper``, preserving the
``{df, status, n_pages, error}`` contract that the ledger and the eval expect.

(The former HTTP + JSON-LD path and the manual /scrape-realestate route were
removed once the browser scraper became the production ingestion path.)
"""

from __future__ import annotations


def sweep_unit(locality: str, deal: str, max_pages: int = 1,
               ptype: str = "byty", **kwargs) -> dict:
    """Sweep one (locality, deal) unit via the headless-browser scraper."""
    from realestate.browser_scraper import sweep_unit as _browser_sweep
    return _browser_sweep(locality, deal, max_pages=max_pages, ptype=ptype, **kwargs)
