"""Model wire input crosses the real service/inspector boundary without research."""

import json
import sys
from datetime import date, timedelta
from types import ModuleType
from typing import Literal

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from core.apps.strategies import parameters, service


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    class Params(BaseModel):
        model_config = ConfigDict(extra="allow", serialize_by_alias=True)
        count: int = Field(alias="n_select", serialization_alias="reported_count", gt=0)

    class Analysis(Params):
        start: date
        end: date
        universe: dict
        config: dict
        optimize: str = "discarded:OptimizeAlgo"

    builds = []

    class EmptyParams(BaseModel):
        pass

    class ModelForm(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        start: date = Field(validation_alias=AliasChoices("start_on", "legacy_start"))
        end: date = Field(alias="end_on")
        cash: float = Field(default=250_000, alias="initial_cash", gt=0, exclude=True)
        pool: Literal["沪深 300", "上证 50"] = Field(default="上证 50", alias="stock_pool")
        lookback: timedelta = Field(default=timedelta(days=5), alias="history")
        count: int = Field(default=17, alias="selection_count", gt=0)

        def build(self):
            analysis = Analysis(
                n_select=self.count, start=self.start, end=self.end,
                universe={"pool": "not the Form pool", "lookback": "P99D"},
                config={"cash": 999}, private_note="not a common or runtime field",
            )
            builds.append(("model", self, analysis))
            return analysis

    class DownstreamForm(BaseModel):
        model_config = ConfigDict(extra="forbid")
        start: date = Field(alias="begin")
        end: date = Field(alias="finish")
        cash: float = Field(default=1_000_000, alias="capital", gt=0)
        pool: Literal["沪深 300", "上证 50"] = Field(default="沪深 300", alias="stocks")
        lookback: timedelta = Field(default=timedelta(0), alias="past")
        model: str = Field(alias="selected_model", min_length=1, json_schema_extra={"x-algo-kind": "model"})
        window: int = Field(default=20, alias="start", gt=0)

        def build(self):
            builds.append(("optimize", self, None))
            return EmptyParams()

    class EmptyForm(BaseModel):
        model_config = ConfigDict(extra="forbid")

        def build(self):
            return EmptyParams()

    for name in ("scheme", "scheme.execute", "scheme.execute.strategy"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    assembly = ModuleType("scheme.execute.strategy.assembly")
    runtime = {}
    # Frozen SDK exports only the legacy discovery API.
    assembly.parameter_type = lambda algo: runtime[algo]
    monkeypatch.setitem(sys.modules, assembly.__name__, assembly)

    components = {}
    for kind, form in (
        ("model", ModelForm), ("optimize", DownstreamForm),
        ("control", EmptyForm), ("execution", EmptyForm),
    ):
        module = ModuleType(f"alias_handoff_{kind}")
        algo = type(f"{kind.title()}Algo", (), {})
        setattr(module, algo.__name__, algo)
        setattr(module, f"{kind.title()}ReportForm", form)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        runtime[algo] = Params if kind == "model" else EmptyParams
        source = tmp_path / kind
        source.mkdir()
        wheel = source / f"{module.__name__}-1.2.1-py3-none-any.whl"
        wheel.write_bytes(b"local frozen candidate")
        (source / "pyproject.toml").write_text('[tool.uv.sources]\nscheme = {path="scheme.whl"}\n')
        (source / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
        components[kind] = {
            "version_id": f"selected-{kind}", "scheme_version": "1.2.0",
            "input": {"environment": {"lockfile": str(source / "uv.lock")}, "backtest": {}},
            "component": {
                "entry": f"{module.__name__}:{algo.__name__}",
                "package": module.__name__, "version": "1.2.1", "wheel": str(wheel),
            },
        }

    payloads = []

    def command(args, cwd, data=None):
        assert cwd.resolve().is_relative_to(tmp_path.resolve())
        if args[:2] == ["uv", "sync"]:
            return ""
        if args[:2] == ["uv", "lock"]:
            (cwd / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
            return ""
        assert args[1].replace("\\", "/").endswith("/strategies/parameters.py")
        payloads.append(json.loads(json.dumps(data)))
        return json.dumps(parameters.inspect(data))

    monkeypatch.setattr(service, "require_components", lambda _: None)
    monkeypatch.setattr(service, "scheme_source", lambda _: ("1.2.0", [{"path": str(tmp_path / "scheme.whl")}]))
    (tmp_path / "scheme.whl").write_bytes(b"local frozen SDK")
    monkeypatch.setattr(service, "command", command)
    return components, builds, payloads


@pytest.mark.parametrize("submit_defaults", [False, True])
def test_build_hands_validated_model_common_to_differently_aliased_downstream(
    handoff, tmp_path, submit_defaults,
):
    components, builds, payloads = handoff
    definition = service.inspect_component(components, "model")
    assert {"start_on", "end_on", "initial_cash", "stock_pool", "history"} <= set(definition["schema"]["properties"])
    values = {"start_on": "2026-06-01", "end_on": "2026-06-03"}
    if submit_defaults:
        values = {**definition["values"], **values, "initial_cash": "123000", "history": "P8D"}
    downstream = service.inspect_component(components, "optimize")
    assert downstream["values"] == {"start": 20}
    forms = {"model": values, "optimize": {"start": 23}, "control": {}, "execution": {}}
    original = json.loads(json.dumps(forms))
    payloads.clear()
    destination = tmp_path / "strategy"
    assert service.build(components, forms, destination) == "1.2.0"
    assert forms == original
    assert [data["kind"] for data in payloads] == list(service.KINDS)
    assert sum(kind == "model" for kind, *_ in builds) == 1
    expected = {
        "start": "2026-06-01", "end": "2026-06-03", "pool": "上证 50",
        "lookback": "P8D" if submit_defaults else "P5D",
        "cash": 123_000 if submit_defaults else 250_000,
    }
    for payload in payloads[1:]:
        assert payload["common"] == expected
    optimize = next(form for kind, form, _ in builds if kind == "optimize")
    assert optimize.start == date(2026, 6, 1)
    assert optimize.end == date(2026, 6, 3)
    assert optimize.cash == expected["cash"]
    assert optimize.pool == "上证 50"
    assert optimize.lookback == timedelta(days=8 if submit_defaults else 5)
    assert optimize.window == 23
    assert optimize.model == components["model"]["component"]["entry"]
    frozen = json.loads((destination / "input.json").read_text())
    assert frozen["backtest"]["count"] == 17
    assert "reported_count" not in frozen["backtest"]
    assert not set(service.KINDS) & set(frozen["backtest"])


@pytest.mark.parametrize("values,error", [
    ({"start_on": "2026-06-01", "end_on": "2026-06-03", "initial_cash": 0}, ValidationError),
    ({"start_on": "2026-06-01", "end_on": "2026-06-03", "unknown": 99}, ValueError),
])
def test_build_rejects_model_invalid_or_unknown_wire_fields_before_downstream(
    handoff, tmp_path, values, error,
):
    components, _, payloads = handoff
    forms = {"model": values, "optimize": {}, "control": {}, "execution": {}}
    with pytest.raises(error):
        service.build(components, forms, tmp_path / "strategy")
    assert [data["kind"] for data in payloads] == ["model"]
    assert not (tmp_path / "strategy").exists()
