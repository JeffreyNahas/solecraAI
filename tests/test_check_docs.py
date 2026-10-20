from scripts.check_docs import missing_docs

MAPPING = [
    {"doc": "docs/data-model.md", "code": ["app/db/models.py", "app/db/migrations/versions/*"]},
    {"doc": "docs/workflows.md", "code": ["app/graphs/*"]},
]


def test_flags_code_without_doc():
    changed = {"app/db/models.py", "app/graphs/outreach.py", "docs/workflows.md"}
    assert missing_docs(changed, MAPPING) == {"docs/data-model.md": ["app/db/models.py"]}


def test_passes_when_docs_updated_or_unmapped():
    assert missing_docs({"app/db/models.py", "docs/data-model.md", "README.md"}, MAPPING) == {}
