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


def _form_schema(model: type[BaseModel]) -> tuple[dict, dict[str, str]]:
    """Map canonical business fields to the flat validation schema's wire keys."""
    by_alias = model.model_config.get("validate_by_alias") is not False
    schema = model.model_json_schema(by_alias=by_alias, mode="validation")
    properties = schema.get("properties", {})
    wire = {}
    for name, field in model.model_fields.items():
        alias = field.validation_alias if by_alias else None
        if isinstance(alias, AliasChoices):
            alias = next((choice for choice in alias.choices if isinstance(choice, str)), None)
            if alias is None:
                raise ValueError(f"Form 字段 {name!r} 的 AliasPath 不支持扁平表单 JSON（unsupported）")
        if isinstance(alias, AliasPath):
            raise ValueError(f"Form 字段 {name!r} 的 AliasPath 不支持扁平表单 JSON（unsupported）")
        key = alias if isinstance(alias, str) else name
        if key not in properties or key in wire.values():
            raise ValueError(f"Form 字段 {name!r} 无法唯一对应 schema wire key {key!r}（unsupported）")
        wire[name] = key
    return schema, wire


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
    schema, wire = _form_schema(form)
    published = {wire[name] for name in names}
    if "values" not in data:
        schema["properties"] = {key: value for key, value in schema["properties"].items() if key in published}
        schema["required"] = [key for key in schema.get("required", []) if key in published]
        values = {
            wire[name]: TypeAdapter(field.annotation).dump_python(field.get_default(call_default_factory=True), mode="json")
            for name, field in form.model_fields.items() if name in names and not field.is_required()
        }
        saved = data.get("saved", {})
        values.update({wire[name]: value for name, value in saved.items() if name in names})
        if kind == "model":
            values.update({wire[name]: value for name, value in saved.get("config", {}).items() if name in names})
            if "pool" in names and saved.get("universe", {}).get("pool"):
                values[wire["pool"]] = saved["universe"]["pool"]
            if "lookback" in names and "lookback" in saved.get("universe", {}):
                values[wire["lookback"]] = saved["universe"]["lookback"]
        return {"schema": schema, "values": values}
    unknown = set(data["values"]) - published
    if unknown:
        raise ValueError(f"{kind} Form 包含未声明字段：{sorted(unknown)}")
    if data.get("entry"):
        values = {
            wire[name]: value for name, value in data.get("common", {}).items()
            if name in COMMON and name in wire
        }
        values.update(data["values"])
        for name, field in form.model_fields.items():
            if (field.json_schema_extra or {}).get("x-algo-kind"):
                values[wire[name]] = data["entries"].get(name) or f"default:{name}"
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
