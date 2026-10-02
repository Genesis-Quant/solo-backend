"""在所选版本的隔离 Python 环境读取和验证 Form，不执行回测。"""
import importlib
import json
import sys
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from pydantic import AliasChoices, AliasPath, BaseModel, TypeAdapter

COMMON = {"start", "end", "pool", "lookback", "market_data", "benchmark", "batch_days", "cash", "commission", "tax"}
STAGES = {"model", "optimize", "control", "execution"}


def _has_alias_path(value: Any, path: Sequence[str | int]) -> bool:
    """Match attribute-mode lookup without treating an invalid value as missing."""
    for key in path:
        if isinstance(value, str):
            return False
        try:
            if isinstance(value, dict) or isinstance(key, int):
                value = value[key]
            else:
                value = getattr(value, key)
        except (AttributeError, KeyError, IndexError, TypeError):
            return False
    return True


def _with_alias_path(value: Any, path: Sequence[str | int], field_value: Any) -> Any:
    """Copy only alias-path containers; never mutate the source's nested values."""
    if not path:
        return field_value
    key, *rest = path
    if isinstance(value, (list, tuple)) and isinstance(key, int) and -len(value) <= key < len(value):
        items = list(value)
        items[key] = _with_alias_path(items[key], rest, field_value)
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, BaseModel) and isinstance(key, str):
        return value.model_copy(update={
            key: _with_alias_path(getattr(value, key, None), rest, field_value),
        })
    items = dict(value) if isinstance(value, dict) else {}
    items[key] = _with_alias_path(items.get(key), rest, field_value)
    return items


def _project_parameters(model: type[BaseModel], source: BaseModel) -> BaseModel:
    # dict(model) retains raw nested models and fields excluded from serialization.
    values = dict(source)
    canonical = isinstance(source, model)
    for name, field in model.model_fields.items():
        if canonical and hasattr(source, name):
            values[name] = getattr(source, name)
        if name not in values or model.model_config.get("validate_by_alias") is False:
            continue
        alias = field.validation_alias
        if alias is None:
            continue
        choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
        paths = [choice.path if isinstance(choice, AliasPath) else [choice] for choice in choices]
        if canonical or not any(_has_alias_path(values, path) for path in paths):
            values = _with_alias_path(values, paths[0], values[name])
    # Attribute validation rebuilds the concrete model and ignores undeclared extras.
    return model.model_validate(SimpleNamespace(**values), from_attributes=True)


def inspect(data: dict) -> dict:
    from scheme.execute.strategy.assembly import parameter_type

    kind = data["kind"]
    if data.get("entry"):
        module_name, name = data["entry"].split(":")
        module = importlib.import_module(module_name)
        algo = getattr(module, name)
        form = getattr(module, kind.title() + "ReportForm")
    else:
        module = importlib.import_module(f"scheme.base.projects.{kind}.default")
        algo = getattr(module, {"optimize": "RiskParity", "control": "NoControl", "execution": "DirectExecution"}[kind])
        form = parameter_type(algo)
    names = set(form.model_fields) - STAGES
    if kind != "model":
        names -= COMMON
    if "values" not in data:
        schema = form.model_json_schema()
        schema["properties"] = {key: value for key, value in schema["properties"].items() if key in names}
        schema["required"] = [name for name in schema.get("required", []) if name in names]
        values = {
            name: TypeAdapter(field.annotation).dump_python(field.get_default(call_default_factory=True), mode="json")
            for name, field in form.model_fields.items() if name in names and not field.is_required()
        }
        saved = data.get("saved", {})
        values.update({key: value for key, value in saved.items() if key in names})
        if kind == "model":
            values.update({key: value for key, value in saved.get("config", {}).items() if key in names})
            if saved.get("universe", {}).get("pool"):
                values["pool"] = saved["universe"]["pool"]
            if "lookback" in saved.get("universe", {}):
                values["lookback"] = saved["universe"]["lookback"]
        return {"schema": schema, "values": values}
    unknown = set(data["values"]) - names
    if unknown:
        raise ValueError(f"{kind} Form 包含未声明字段：{sorted(unknown)}")
    values = {**data.get("common", {}), **data["values"]}
    if data.get("entry"):
        values = {key: value for key, value in values.items() if key in form.model_fields}
        for name, field in form.model_fields.items():
            if (field.json_schema_extra or {}).get("x-algo-kind"):
                values[name] = data["entries"].get(name) or f"default:{name}"
        built = form.model_validate(values).build()
        # 该脚本也在旧 Scheme 的隔离环境中运行，不依赖新增的投影辅助函数。
        params = _project_parameters(parameter_type(algo), built)
        return {"backtest": built.model_dump(mode="json"), "params": params.model_dump(mode="json")}
    return {"params": form.model_validate(data["values"]).model_dump(mode="json")}


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
