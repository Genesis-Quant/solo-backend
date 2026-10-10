import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from core.apps.projects.templates import unpack_template


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
def test_tagged_research_notebook_preserves_code_outputs_and_metadata(kind, tmp_path):
    prefix = kind.title()
    algo = "Factor" if kind == "factor" else f"{prefix}Algo"
    form = f"{prefix}ReportForm"
    original = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"},
            "language_info": {"name": "python", "version": "3.12"},
            "custom": {"preserve": True, "example": f"from {kind} import {algo}"},
        },
        "cells": [
            {"cell_type": "markdown", "id": "instructions", "metadata": {"collapsed": False},
             "source": ["# 研究\n", "填写参数后逐格运行；上游参数和报告保存都保留。\n",
                        f"示例：from {kind} import {algo}"]},
            {"cell_type": "code", "id": "imports", "execution_count": None, "metadata": {},
             "source": [f"from {kind} import {algo}, {form}\n", "from scheme import algo_options"],
             "outputs": []},
            {"cell_type": "code", "id": "parameters", "execution_count": 1, "metadata": {},
             "source": [f'form = {form}(start="2026-06-01", end="2026-06-06")\n',
                        "params = form.build()"], "outputs": []},
            {"cell_type": "code", "id": "run-report", "execution_count": 2,
             "metadata": {"jupyter": {"outputs_hidden": True}, "example": f"from {kind} import {algo}"},
             "source": [f"report = params.run({algo})\n", "report.show()\n",
                        "paths = report.save(output)"],
             "outputs": [
                 {"output_type": "stream", "name": "stdout", "text": f"from {kind} import {algo}\n"},
                 {"output_type": "display_data", "metadata": {"example": f"import {kind}"},
                  "data": {"text/plain": [f"import {kind}\n"]}},
             ]},
            {"cell_type": "code", "id": "user-work", "execution_count": None, "metadata": {},
             "source": ["custom_parameters = {'n_select': 7}"], "outputs": []},
            {"cell_type": "raw", "id": "original-text", "metadata": {"example": f"import {kind}"},
             "source": f"from {kind} import {algo}\nimport {kind}\n"},
        ],
    }
    source = BytesIO()
    with ZipFile(source, "w") as archive:
        archive.writestr(f"repo/{kind}/pyproject.toml", f'[project]\nname="{kind}"\nversion="1.2.1"\n')
        archive.writestr(f"repo/{kind}/research.ipynb", json.dumps(original))
        archive.writestr(f"repo/{kind}/src/{kind}/__init__.py", "")
    unpack_template(source.getvalue(), kind, tmp_path, "abcd")
    notebook = json.loads((tmp_path / "research.ipynb").read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(original))
    expected["cells"][1]["source"][0] = f"from {kind}_abcd import {algo}, {form}\n"
    assert notebook == expected
    assert (tmp_path / "src" / f"{kind}_abcd" / "__init__.py").is_file()


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
@pytest.mark.parametrize("layout", ["str", "lines", "chunks", "empty"], ids=["source-str", "source-lines", "source-chunks", "source-empty"])
def test_notebook_import_rename_preserves_source_type_and_list_segments(kind, layout, tmp_path):
    if layout == "str":
        source = (f"from {kind} import Algo\nimport {kind} as current\n"
                  f"from {kind}.params import Params\nimport {kind}_dependency\nvalue = 1\n")
        renamed = (f"from {kind}_abcd import Algo\nimport {kind}_abcd as current\n"
                   f"from {kind}_abcd.params import Params\nimport {kind}_dependency\nvalue = 1\n")
    elif layout == "lines":
        source = [f"from {kind} import Algo\n", f"import {kind} as current\n\n",
                  f"from {kind}.params import Params\n", f"import {kind}_dependency\n", "value = 1"]
        renamed = [f"from {kind}_abcd import Algo\n", f"import {kind}_abcd as current\n\n",
                   f"from {kind}_abcd.params import Params\n", f"import {kind}_dependency\n", "value = 1"]
    elif layout == "chunks":
        # A Notebook source list can split tokens rather than use one item per line.
        source = ["fr", "om ", kind[:2], "", kind[2:], " import Algo\nimport ", kind,
                  " as current\nfrom ", kind, ".params import Params\n", "import ",
                  f"{kind}_dependency\n", "value = 1", ""]
        renamed = ["fr", "om ", kind[:2], "", kind[2:] + "_abcd", " import Algo\nimport ",
                   kind + "_abcd", " as current\nfrom ", kind + "_abcd", ".params import Params\n",
                   "import ", f"{kind}_dependency\n", "value = 1", ""]
    else:
        source, renamed = [], []
    original = {
        "nbformat": 4, "nbformat_minor": 5, "metadata": {"example": f"import {kind}"},
        "cells": [{"cell_type": "code", "id": "original-imports", "execution_count": 7,
                   "metadata": {"example": f"from {kind} import Algo"}, "source": source,
                   "outputs": [{"output_type": "stream", "name": "stdout", "text": f"import {kind}\n"}]}],
    }
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(f"repo/{kind}/pyproject.toml", f'[project]\nname="{kind}"\nversion="1.2.1"\n')
        archive.writestr(f"repo/{kind}/research.ipynb", json.dumps(original))
    unpack_template(content.getvalue(), kind, tmp_path, "abcd")
    actual = json.loads((tmp_path / "research.ipynb").read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(original))
    expected["cells"][0]["source"] = renamed
    assert actual == expected
    assert type(actual["cells"][0]["source"]) is type(source)
    if isinstance(source, list):
        assert len(actual["cells"][0]["source"]) == len(source)


def test_unpacking_does_not_replace_an_empty_tagged_notebook_with_generated_cells(tmp_path):
    source = BytesIO()
    with ZipFile(source, "w") as archive:
        archive.writestr("repo/model/pyproject.toml", '[project]\nname="model"\nversion="1.2.1"\n')
        archive.writestr("repo/model/research.ipynb", '{"cells": [], "metadata": {}}')
    unpack_template(source.getvalue(), "model", tmp_path, "abcd")
    notebook = json.loads((tmp_path / "research.ipynb").read_text(encoding="utf-8"))
    assert notebook == {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
