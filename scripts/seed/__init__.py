"""Seed data loader.

Reads a snapshot directory (extracted from prod via a one-shot script)
and idempotently materializes it into the local stack: DB rows + MinIO
objects.  Source schema diffs from current dev (prod runs older code),
so the loader fills required fields with stubs where prod didn't carry
them — see ``load.py`` for the full mapping.
"""
