"""隔离 Scheme 表单的参数投影，不运行环境安装、回测或持久化。"""

import sys
from datetime import date, timedelta
from types import ModuleType

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from core.apps.strategies.parameters import _project_parameters, inspect


@pytest.fixture
def selected_form(monkeypatch):
    def install(kind, params, form):
        module = ModuleType("strategy_parameters_fixture")
        algo = type(f"{kind.title()}Algo", (), {})
        setattr(module, algo.__name__, algo)
        setattr(module, f"{kind.title()}ReportForm", form)
        assembly = ModuleType("scheme.execute.strategy.assembly")
        # 旧 Scheme 只提供 parameter_type，不能导入新增的 project_parameters。
        assembly.parameter_type = lambda cls: params
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setitem(sys.modules, assembly.__name__, assembly)
        return {"kind": kind, "entry": f"{module.__name__}:{algo.__name__}", "entries": {}}

    return install


def test_subclass_projection_excludes_analysis_and_selector_fields(selected_form):
    class Params(BaseModel):
        model_config = ConfigDict(extra="allow")
        window: int = Field(gt=0)

    class Analysis(Params):
        start: date
        end: date
        config: dict[str, float]
        model: str

    class Form(BaseModel):
        start: date
        end: date
        cash: float
        window: int
        model: str = Field(json_schema_extra={"x-algo-kind": "model"})

        def build(self):
            return Analysis(
                start=self.start, end=self.end, window=self.window,
                config={"cash": self.cash}, model=self.model,
            )

    data = selected_form("optimize", Params, Form)
    data.update(
        values={"window": 17},
        common={"start": "2026-06-01", "end": "2026-06-03", "cash": 123_000},
        entries={"model": "selected_model:ModelAlgo"},
    )
    result = inspect(data)
    assert result["params"] == {"window": 17}
    assert result["backtest"] == {
        "start": "2026-06-01", "end": "2026-06-03", "window": 17,
        "config": {"cash": 123_000}, "model": "selected_model:ModelAlgo",
    }


def test_projection_validates_missing_required_runtime_fields(selected_form):
    class Params(BaseModel):
        window: int

    class Form(BaseModel):
        def build(self):
            return self

    with pytest.raises(ValidationError, match="window"):
        inspect({**selected_form("model", Params, Form), "values": {}})


def test_projection_preserves_runtime_defaults(selected_form):
    class Params(BaseModel):
        window: int = 20

    class Form(BaseModel):
        def build(self):
            return self

    result = inspect({**selected_form("model", Params, Form), "values": {}})
    assert result == {"backtest": {}, "params": {"window": 20}}


@pytest.mark.parametrize("canonical", [False, True])
def test_selected_form_preserves_alias_in_legacy_and_canonical_analysis(selected_form, canonical):
    class Params(BaseModel):
        model_config = ConfigDict(extra="allow")
        count: int = Field(alias="n_select", gt=0)

    class LegacyAnalysis(BaseModel):
        model_config = ConfigDict(extra="allow")

    class Analysis(Params):
        study: str = "research only"

    class Form(BaseModel):
        n_select: int

        def build(self):
            analysis = Analysis if canonical else LegacyAnalysis
            return analysis(n_select=self.n_select, private_note="not runtime")

    result = inspect({**selected_form("model", Params, Form), "values": {"n_select": 17}})
    assert result["params"] == {"count": 17}
    assert result["backtest"]["private_note"] == "not runtime"
    assert result["backtest"]["count" if canonical else "n_select"] == 17


@pytest.mark.parametrize("alias,inputs", [
    ("n_select", {"n_select": 17}),
    (AliasChoices("n_select", "selection"), {"selection": 17}),
    (AliasPath("config", "counts", -1), {"config": {"counts": [3, 17]}}),
    (AliasChoices(AliasPath("config", "count"), "n_select"), {"n_select": 17}),
])
def test_projection_preserves_alias_precedence_and_canonical_fallback(alias, inputs):
    class Params(BaseModel):
        model_config = ConfigDict(extra="allow")
        count: int = Field(validation_alias=alias, gt=0)

    class LegacyAnalysis(BaseModel):
        model_config = ConfigDict(extra="allow")

    class Analysis(Params):
        study: str = "research only"

    legacy = LegacyAnalysis(count=99, unrelated=True, **inputs)
    expected = Params.model_validate(legacy, from_attributes=True)
    projected = _project_parameters(Params, legacy)
    assert type(projected) is Params
    assert projected.count == expected.count == 17
    assert projected.model_extra == {}
    assert _project_parameters(Params, LegacyAnalysis(count=17)).count == 17

    canonical = Analysis(**inputs)
    if "config" in inputs:
        canonical.config = inputs["config"]
    canonical.count = 23
    projected = _project_parameters(Params, canonical)
    assert type(projected) is Params
    assert projected is not canonical
    assert projected.count == 23
    assert projected.model_extra == {}
    if "config" in inputs:
        assert canonical.model_extra["config"] == inputs["config"]
    canonical.count = 0
    with pytest.raises(ValidationError):
        _project_parameters(Params, canonical)
    with pytest.raises(ValidationError):
        _project_parameters(Params, LegacyAnalysis())


def test_projection_keeps_raw_nested_subtype_and_excluded_field():
    class Nested(BaseModel):
        value: int

    class NestedChild(Nested):
        detail: str

    class Params(BaseModel):
        nested: Nested
        hidden: int = Field(alias="secret", exclude=True)

    class Analysis(Params):
        study: str = "research only"

    nested = NestedChild(value=3, detail="must survive projection")
    analysis = Analysis(nested=nested, secret=17)
    projected = _project_parameters(Params, analysis)
    assert type(projected) is Params
    assert projected.nested is nested
    assert type(projected.nested) is NestedChild
    assert projected.hidden == 17
    assert projected.model_dump() == {"nested": {"value": 3}}


def test_alias_defaults_and_present_invalid_values_are_validated():
    class Params(BaseModel):
        count: int = Field(default=20, alias="n_select", gt=0)
        labels: list[str] = Field(default_factory=list, validation_alias=AliasPath("config", "labels"))

    class LegacyAnalysis(BaseModel):
        model_config = ConfigDict(extra="allow")

    first = _project_parameters(Params, LegacyAnalysis())
    second = _project_parameters(Params, LegacyAnalysis())
    assert first.count == second.count == 20
    assert first.labels == second.labels == []
    assert first.labels is not second.labels
    for invalid in (None, "invalid", 0):
        with pytest.raises(ValidationError):
            _project_parameters(Params, LegacyAnalysis(count=17, n_select=invalid))


def test_model_schema_restores_convenient_saved_fields(selected_form):
    class Form(BaseModel):
        start: date
        end: date
        pool: str = "沪深 300"
        lookback: timedelta = timedelta(0)
        cash: float = 1_000_000
        window: int = 20
        optimize: str = "risk_parity"

    data = selected_form("model", BaseModel, Form)
    data["saved"] = {
        "start": "2026-06-01", "end": "2026-06-03", "window": 17,
        "universe": {"pool": "上证 50", "lookback": "P60D"},
        "config": {"cash": 123_000}, "optimize": "ignored",
    }
    result = inspect(data)
    assert set(result["schema"]["properties"]) == {
        "start", "end", "pool", "lookback", "cash", "window",
    }
    assert result["schema"]["required"] == ["start", "end"]
    assert result["values"] == {
        "start": "2026-06-01", "end": "2026-06-03", "window": 17,
        "pool": "上证 50", "lookback": "P60D", "cash": 123_000,
    }


def test_downstream_schema_hides_shared_inputs_and_selectors(selected_form):
    class Form(BaseModel):
        start: date
        end: date
        pool: str = "沪深 300"
        cash: float = 1_000_000
        model: str = Field(json_schema_extra={"x-algo-kind": "model"})
        window: int = 20

    data = selected_form("optimize", BaseModel, Form)
    result = inspect(data)
    assert set(result["schema"]["properties"]) == {"window"}
    assert result["schema"]["required"] == []
    assert result["values"] == {"window": 20}
    with pytest.raises(ValueError, match="未声明字段.*start"):
        inspect({**data, "values": {"start": "2026-06-01"}})


def test_builtin_default_parameters_keep_legacy_discovery(monkeypatch):
    class Params(BaseModel):
        gross_exposure: float = 0.98

    module = ModuleType("scheme.base.projects.optimize.default")
    module.RiskParity = object
    assembly = ModuleType("scheme.execute.strategy.assembly")
    assembly.parameter_type = lambda cls: Params
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, assembly.__name__, assembly)
    assert inspect({"kind": "optimize"})["values"] == {"gross_exposure": 0.98}
    assert inspect({"kind": "optimize", "values": {"gross_exposure": 0.75}}) == {
        "params": {"gross_exposure": 0.75},
    }
