"""The dataset contract is written down twice, on purpose.

``nova_ml/dataset.py`` and the API's ``dataset_export.py`` each carry the
column tuple and the source names, because the pipeline must not import
the API. Two copies drift; this test reads the API's copy from source and
compares, so a change on either side fails here before it fails in a
training run three weeks later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nova_ml import dataset

API_COPY = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "api"
    / "src"
    / "nova"
    / "services"
    / "dataset_export.py"
)


def _constants(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.isupper():
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return found


@pytest.mark.skipif(not API_COPY.exists(), reason="API source not checked out alongside")
def test_the_api_export_writes_the_columns_the_pipeline_reads() -> None:
    api = _constants(API_COPY)
    assert api["COLUMNS"] == dataset.COLUMNS
    assert api["SOURCE_TELEMETRY"] == dataset.SOURCE_TELEMETRY
    assert api["SOURCE_MESSAGE"] == dataset.SOURCE_MESSAGE
    assert api["MESSAGE_EVENT_TYPE"] == dataset.MESSAGE_EVENT_TYPE
