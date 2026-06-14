"""Fulfillability Engine subsystem.

Pure functions (``is_fulfillable``, ``shortfalls``) computed on read for every
not-yet-Approved order whose products' stock changed. Never stored, so it never
becomes stale relative to stock. Language-agnostic.
"""
