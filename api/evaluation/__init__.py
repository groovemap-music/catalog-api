"""Offline evaluation harness: golden set, in-memory graph, frozen baseline, metrics.

This package exists so today's recommendation and rarity heuristics can be scored
reproducibly, without a database, before any learned model is proposed. It imports the
scoring functions directly from :mod:`api.queries.recommend_queries` and :mod:`api.rarity` and
never touches a driver, a pool, a router, or a network. Everything it needs is the committed
synthetic golden set under ``tests/fixtures/golden``.

The pieces, in the order a run uses them:

* :mod:`api.evaluation.fixtures` — load the committed golden set.
* :mod:`api.evaluation.graph` — answer, from that fixture, the exact dict shapes the
  DB-bound query functions return.
* :mod:`api.evaluation.split` — hold out each collector's later acquisitions.
* :mod:`api.evaluation.baseline` — the frozen weight tables and the run that applies them.
* :mod:`api.evaluation.metrics` — precision, recall, coverage, breakdowns, rank stability.
* :mod:`api.evaluation.report` — assemble a run into a report; ``just evaluate`` runs it.

Raw run artifacts are written to ``reports/``, which is gitignored: the repository keeps the
recipe and one small expected-metrics snapshot, never the raw output of a run.
"""

from __future__ import annotations
