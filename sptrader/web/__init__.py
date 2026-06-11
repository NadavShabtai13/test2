"""Web dashboard for browsing optimization results.

Serves a single page that lists every strategy whose trade-level success rate
clears a configurable threshold (70% by default), with the contributing
indicators broken out per row. Backed by the same Postgres tables the search
writes to, so it can be opened live while an ``optimize`` run is still going.
"""
