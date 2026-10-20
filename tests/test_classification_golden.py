"""Golden classification table (RULE-BASED) — SKIPPED.

Rule-based classification was removed in v2-combined: classification of a
thesis now comes from the LLM inside the single generation call, validated
against the 29 ISCED-F 2013 names in ``app.news.isced`` and stored per record
in ``main_category_matches`` (see ``tests/test_combined_classification.py``).

The original golden table lived in git history (this file, before the v2
rework) and remains recoverable for:
- ``scripts/reclassify_fields.py`` audits of historical rows;
- judging future disputes like the old ``classification-conflicts.csv``.
"""

import pytest

pytest.skip(
    "rule-based classification removed (v2-combined): the LLM classifies inside "
    "the generation call against the ISCED-F 2013 vocabulary",
    allow_module_level=True,
)
