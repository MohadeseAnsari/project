"""Field-name normalization.

Deterministic Persian normalization only (``normalize_field``): the stable
identity for field dedup. Classification RULES live in
``app.fields.classification`` but that module is DEPRECATED — classification
is produced by the combined generation call per record (see
``app/services/records.py``); the module is retained only as reference.
"""
