import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from core.apps.projects.templates import unpack_template


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
def test_tagged_template_demonstrations_are_not_copied_into_new_research(kind, tmp_path):
    source = BytesIO()
    original = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}},
        "cells": [{"cell_type": "code", "execution_count": 12, "metadata": {},
                   "source": ["report.save('/shared/runs/factor/demo/example')"],
                   "outputs": [{"output_type": "stream", "name": "stdout", "text": "demonstration"}]}],
    }
    with ZipFile(source, "w") as archive:
        archive.writestr(f"repo/{kind}/pyproject.toml", f'[project]\nname="{kind}"\nversion="1.2.1"\n')
        archive.writestr(f"repo/{kind}/research.ipynb", json.dumps(original))
        archive.writestr(f"repo/{kind}/src/{kind}/__init__.py", "")
    unpack_template(source.getvalue(), kind, tmp_path, "abcd")
    notebook = json.loads((tmp_path / "research.ipynb").read_text(encoding="utf-8"))
    assert notebook["metadata"] == original["metadata"]
    assert notebook["nbformat"] == 4 and notebook["nbformat_minor"] == 5
    code = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
    assert len(code) == 2
    assert code[0]["source"][0].startswith(f"from {kind}_abcd import ")
    assert code[1]["source"] == []
    assert all(cell["execution_count"] is None and cell["outputs"] == [] for cell in code)
    assert "report.save" not in json.dumps(notebook) and "/example" not in json.dumps(notebook)
    assert (tmp_path / "src" / f"{kind}_abcd" / "__init__.py").is_file()
