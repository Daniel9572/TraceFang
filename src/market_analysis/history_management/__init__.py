"""Frozen historical-governance research package.

It is deliberately provider-agnostic, has no network clients, and is not wired
into the application runtime. Current market queries use the contract-bound
realtime source instead; do not use this package to form a cross-source series.
"""
