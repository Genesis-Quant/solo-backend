"""隔离 Scheme 表单的参数投影，不运行环境安装、回测或持久化。"""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from textwrap import dedent
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
    assert result == {"backtest": {}, "params": {"window": 20}, "common": {}}


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


@pytest.mark.parametrize("kind", ["model", "optimize"])
def test_legacy_frozen_form_alias_round_trips_defaults_saved_and_submit(selected_form, kind):
    class Params(BaseModel):
        window: int = Field(gt=0)

    class Form(BaseModel):
        model_config = ConfigDict(frozen=True, populate_by_name=True)
        window: int = Field(default=20, alias="lookback_days", gt=0)

        def build(self):
            return Params(window=self.window)

    data = selected_form(kind, Params, Form)
    definition = inspect(data)
    assert set(definition["schema"]["properties"]) == {"lookback_days"}
    assert definition["schema"]["properties"]["lookback_days"]["default"] == 20
    assert definition["schema"]["required"] == []
    assert definition["values"] == {"lookback_days": 20}
    restored = inspect({**data, "saved": {"window": 17, "lookback_days": 99, "other": 5}})
    assert restored["values"] == {"lookback_days": 17}
    result = inspect({**data, "values": restored["values"]})
    assert result == {
        "backtest": {"window": 17}, "params": {"window": 17},
        **({"common": {}} if kind == "model" else {}),
    }
    with pytest.raises(ValueError, match="未声明字段.*window"):
        inspect({**data, "values": {"window": 17}})
    with pytest.raises(ValidationError, match="lookback_days"):
        inspect({**data, "values": {"lookback_days": 0}})


def test_standalone_script_round_trips_frozen_form_with_only_legacy_scheme_api():
    script = Path(__file__).resolve().parents[1] / "core/apps/strategies/parameters.py"
    bootstrap = dedent("""\
        import runpy
        import sys
        from types import ModuleType
        from pydantic import BaseModel, ConfigDict, Field

        for name in ("scheme", "scheme.execute", "scheme.execute.strategy"):
            package = ModuleType(name)
            package.__path__ = []
            sys.modules[name] = package

        class Params(BaseModel):
            window: int = Field(validation_alias="runtime_days", gt=0)

        class FrozenAnalysis(BaseModel):
            model_config = ConfigDict(frozen=True)
            window: int
            study: str = "not a runtime field"

        class FrozenForm(BaseModel):
            model_config = ConfigDict(frozen=True)
            window: int = Field(default=20, alias="lookback_days", gt=0)

            def build(self):
                return FrozenAnalysis(window=self.window)

        module = ModuleType("frozen_form_fixture")
        module.LegacyAlgo = type("LegacyAlgo", (), {})
        module.ModelReportForm = FrozenForm
        sys.modules[module.__name__] = module
        assembly = ModuleType("scheme.execute.strategy.assembly")
        assembly.parameter_type = lambda cls: Params
        sys.modules[assembly.__name__] = assembly
        runpy.run_path(sys.argv[1], run_name="__main__")
    """)
    data = {"kind": "model", "entry": "frozen_form_fixture:LegacyAlgo", "entries": {}}

    def execute(payload):
        return subprocess.run(
            [sys.executable, "-B", "-c", bootstrap, str(script)],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False,
        )

    definition = execute({**data, "saved": {"window": 17}})
    assert definition.returncode == 0, definition.stderr
    restored = json.loads(definition.stdout)
    assert restored["schema"]["properties"]["lookback_days"]["default"] == 20
    assert restored["values"] == {"lookback_days": 17}
    submitted = execute({**data, "values": restored["values"]})
    assert submitted.returncode == 0, submitted.stderr
    assert json.loads(submitted.stdout) == {
        "backtest": {"window": 17, "study": "not a runtime field"},
        "params": {"window": 17}, "common": {},
    }
    rejected = execute({**data, "values": {"window": 17}})
    assert rejected.returncode == 1
    assert "window" in rejected.stderr
    assert not rejected.stdout


@pytest.mark.parametrize("alias", [
    "lookback_days",
    AliasChoices("lookback_days", "legacy_days"),
    AliasChoices(AliasPath("config", "window"), "lookback_days", "legacy_days"),
])
def test_form_uses_published_validation_alias_not_serialization_alias(selected_form, alias):
    class Params(BaseModel):
        window: int

    class Form(BaseModel):
        model_config = ConfigDict(extra="allow", populate_by_name=True)
        window: int = Field(validation_alias=alias, serialization_alias="serialized_days")

        def build(self):
            return Params(window=self.window)

    data = selected_form("optimize", Params, Form)
    definition = inspect({**data, "saved": {"window": 17}})
    assert set(definition["schema"]["properties"]) == {"lookback_days"}
    assert definition["schema"]["required"] == ["lookback_days"]
    assert definition["values"] == {"lookback_days": 17}
    assert inspect({**data, "values": definition["values"]})["params"] == {"window": 17}
    for unknown in ("window", "serialized_days", "legacy_days", "config", "other"):
        with pytest.raises(ValueError, match=f"未声明字段.*{unknown}"):
            inspect({**data, "values": {"lookback_days": 17, unknown: 99}})


def test_alias_classification_and_injected_common_selectors_use_canonical_names(selected_form):
    class Params(BaseModel):
        window: int

    class Analysis(Params):
        start: date
        end: date
        cash: float
        model: str

    class Form(BaseModel):
        model_config = ConfigDict(extra="forbid")
        start: date = Field(alias="start_on")
        end: date = Field(alias="end_on")
        cash: float = Field(alias="initial_cash")
        model: str = Field(alias="selected_model", json_schema_extra={"x-algo-kind": "model"})
        window: int = Field(default=20, alias="start")

        def build(self):
            return Analysis(**self.model_dump())

    data = selected_form("optimize", Params, Form)
    definition = inspect(data)
    assert set(definition["schema"]["properties"]) == {"start"}
    assert definition["values"] == {"start": 20}
    for hidden in ("start_on", "end_on", "initial_cash", "selected_model"):
        with pytest.raises(ValueError, match=f"未声明字段.*{hidden}"):
            inspect({**data, "values": {hidden: "not a visible parameter"}})
    result = inspect({
        **data, "values": {"start": 17},
        "common": {
            "start": "2026-06-01", "end": "2026-06-03", "cash": 123_000,
            "window": 99, "initial_cash": 99, "unknown": "must not leak",
            "model": "untrusted:ModelAlgo", "selected_model": "untrusted:ModelAlgo",
        },
        "entries": {"model": "selected:ModelAlgo"},
    })
    assert result["params"] == {"window": 17}
    assert result["backtest"] == {
        "start": "2026-06-01", "end": "2026-06-03", "cash": 123_000,
        "window": 17, "model": "selected:ModelAlgo",
    }
    with pytest.raises(ValidationError, match="start_on"):
        inspect({**data, "values": {"start": 17}, "common": {"start_on": "2026-06-01"}})


def test_model_aliases_restore_only_declared_saved_common_fields(selected_form):
    class Form(BaseModel):
        start: date = Field(alias="start_on")
        end: date = Field(alias="end_on")
        pool: str = Field(default="沪深 300", alias="stock_pool")
        lookback: timedelta = Field(default=timedelta(0), alias="history")
        cash: float = Field(default=1_000_000, alias="initial_cash")
        window: int = Field(default=20, alias="lookback_days")
        optimize: str = Field(default="default:optimize", alias="selected_optimize")

    data = selected_form("model", BaseModel, Form)
    saved = {
        "start": "2026-06-01", "end": "2026-06-03", "window": 17,
        "universe": {"pool": "上证 50", "lookback": "P60D"},
        "config": {"cash": 123_000, "unknown": 9}, "optimize": "ignored",
    }
    definition = inspect({**data, "saved": saved})
    assert set(definition["schema"]["properties"]) == {
        "start_on", "end_on", "stock_pool", "history", "initial_cash", "lookback_days",
    }
    assert definition["schema"]["required"] == ["start_on", "end_on"]
    assert definition["values"] == {
        "start_on": "2026-06-01", "end_on": "2026-06-03", "lookback_days": 17,
        "stock_pool": "上证 50", "history": "P60D", "initial_cash": 123_000,
    }

    class MinimalForm(BaseModel):
        window: int = Field(default=20, alias="lookback_days")

    minimal = inspect({**selected_form("model", BaseModel, MinimalForm), "saved": saved})
    assert minimal["values"] == {"lookback_days": 17}


@pytest.mark.parametrize("alias", [
    AliasPath("config", "window"),
    AliasPath("lookback_days"),
    AliasChoices(AliasPath("config", "window"), AliasPath("other", 0)),
])
@pytest.mark.parametrize("submit", [False, True])
def test_form_alias_paths_without_flat_schema_key_fail_explicitly(selected_form, alias, submit):
    class Form(BaseModel):
        window: int = Field(default=20, validation_alias=alias)

        def build(self):
            raise AssertionError("unsupported form must not build")

    data = selected_form("optimize", BaseModel, Form)
    if submit:
        data["values"] = {"window": 17}
    with pytest.raises(ValueError, match="window.*AliasPath.*unsupported"):
        inspect(data)


def test_form_with_alias_validation_disabled_publishes_canonical_key(selected_form):
    class Params(BaseModel):
        window: int

    class Form(BaseModel):
        model_config = ConfigDict(validate_by_alias=False)
        window: int = Field(default=20, alias="lookback_days")

        def build(self):
            return Params(window=self.window)

    data = selected_form("optimize", Params, Form)
    definition = inspect({**data, "saved": {"window": 17}})
    assert set(definition["schema"]["properties"]) == {"window"}
    assert definition["values"] == {"window": 17}
    assert inspect({**data, "values": definition["values"]})["params"] == {"window": 17}
    with pytest.raises(ValueError, match="未声明字段.*lookback_days"):
        inspect({**data, "values": {"lookback_days": 17}})


def test_builtin_alias_parameters_use_the_same_published_wire_key(monkeypatch):
    class Params(BaseModel):
        window: int = Field(default=20, alias="lookback_days")

    module = ModuleType("scheme.base.projects.optimize.default")
    module.RiskParity = object
    assembly = ModuleType("scheme.execute.strategy.assembly")
    assembly.parameter_type = lambda cls: Params
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, assembly.__name__, assembly)
    definition = inspect({"kind": "optimize", "saved": {"window": 17}})
    assert set(definition["schema"]["properties"]) == {"lookback_days"}
    assert definition["values"] == {"lookback_days": 17}
    assert inspect({"kind": "optimize", "values": definition["values"]}) == {
        "params": {"window": 17},
    }
    with pytest.raises(ValueError, match="未声明字段.*window"):
        inspect({"kind": "optimize", "values": {"window": 17}})


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


@pytest.mark.parametrize("outer_alias", [False, True])
@pytest.mark.parametrize("nested_alias", [False, True])
def test_nested_form_alias_scopes_round_trip_schema_defaults_and_submission(
    selected_form, outer_alias, nested_alias,
):
    class Nested(BaseModel):
        model_config = ConfigDict(validate_by_alias=nested_alias, extra="forbid")
        count: int = Field(
            validation_alias=AliasChoices("n_select", "legacy_count"),
            serialization_alias="reported_count", gt=0,
        )

    nested_key = "n_select" if nested_alias else "count"

    class Params(BaseModel):
        counts: list[int]

    class Form(BaseModel):
        model_config = ConfigDict(validate_by_alias=outer_alias, extra="forbid")
        nested: Nested = Field(alias="selection")
        optional: Nested | None = Field(
            default=Nested(**{nested_key: 7}), alias="optional_selection",
        )
        items: list[Nested] = Field(
            default_factory=lambda: [Nested(**{nested_key: 11})], alias="selections",
        )
        empty: Nested | None = Field(default=None, alias="empty_selection")

        def build(self):
            return Params(counts=[
                self.nested.count, self.optional.count, *(item.count for item in self.items),
            ])

    data = selected_form("optimize", Params, Form)
    definition = inspect(data)
    schema = definition["schema"]
    nested_schema = schema["$defs"]["Nested"]
    assert set(nested_schema["properties"]) == {nested_key}
    assert nested_schema["required"] == [nested_key]
    keys = {
        "nested": "selection", "optional": "optional_selection",
        "items": "selections", "empty": "empty_selection",
    } if outer_alias else {name: name for name in Form.model_fields}
    assert set(schema["properties"]) == set(keys.values())
    assert schema["required"] == [keys["nested"]]
    assert schema["properties"][keys["optional"]]["default"] == {nested_key: 7}
    assert schema["properties"][keys["nested"]]["$ref"] == "#/$defs/Nested"
    assert schema["properties"][keys["items"]]["items"]["$ref"] == "#/$defs/Nested"
    assert definition["values"] == {
        keys["optional"]: {nested_key: 7}, keys["items"]: [{nested_key: 11}],
        keys["empty"]: None,
    }
    values = {**definition["values"], keys["nested"]: {nested_key: 17}}
    assert inspect({**data, "values": values})["params"] == {"counts": [17, 7, 11]}
    restored = inspect({**data, "saved": {
        "optional": {"count": 23}, "items": [{"count": 19}],
    }})
    assert restored["values"][keys["optional"]] == {nested_key: 23}
    assert restored["values"][keys["items"]] == [{nested_key: 19}]
    assert inspect({
        **data, "values": {**restored["values"], keys["nested"]: {nested_key: 17}},
    })["params"] == {"counts": [17, 23, 19]}
    with pytest.raises(ValidationError, match=nested_key):
        inspect({**data, "values": {**values, keys["nested"]: {}}})
    with pytest.raises(ValidationError):
        inspect({**data, "values": {
            **values, keys["nested"]: {"count" if nested_alias else "n_select": 17},
        }})
    with pytest.raises(ValidationError, match="undeclared"):
        inspect({**data, "values": {
            **values, keys["nested"]: {nested_key: 17, "undeclared": 99},
        }})


@pytest.mark.parametrize("extra", ["start", "cash", "unknown", "config"])
def test_model_common_snapshot_uses_only_validated_declared_canonical_fields(selected_form, extra):
    class Params(BaseModel):
        model_config = ConfigDict(extra="allow", serialize_by_alias=True)
        window: int = Field(validation_alias="runtime_days", serialization_alias="saved_days")

    class Analysis(Params):
        study: str = "research only"

    built = []

    class Form(BaseModel):
        model_config = ConfigDict(extra="allow", frozen=True)
        start: date = Field(validation_alias=AliasChoices("start_on", "legacy_start"))
        end: date = Field(alias="end_on")
        pool: str = Field(default="沪深 300", alias="stock_pool")
        lookback: timedelta = Field(default=timedelta(days=5), alias="history")
        cash: float = Field(default=1_000_000, alias="initial_cash", gt=0, exclude=True)
        window: int = Field(default=20, alias="market_data")
        optimize: str = Field(default="default:optimize", alias="selected_optimize")

        def build(self):
            analysis = Analysis(runtime_days=self.window, unrelated="must not leak")
            built.append(analysis)
            return analysis

    data = selected_form("model", Params, Form)
    values = {"start_on": "2026-06-01", "end_on": "2026-06-03", "initial_cash": "123000"}
    result = inspect({**data, "values": values})
    assert result["common"] == {
        "start": "2026-06-01", "end": "2026-06-03", "pool": "沪深 300",
        "lookback": "P5D", "cash": 123_000,
    }
    assert result["params"] == {"window": 20}
    assert built[0].model_extra == {"unrelated": "must not leak"}
    assert "window" in result["backtest"] and "saved_days" not in result["backtest"]
    with pytest.raises(ValueError, match=f"未声明字段.*{extra}"):
        inspect({**data, "values": {**values, extra: "must not leak"}})
    with pytest.raises(ValidationError, match="initial_cash"):
        inspect({**data, "values": {**values, "initial_cash": 0}})
