"""在所选版本的隔离 Python 环境读取和验证 Form，不执行回测。"""
import importlib
import json
import sys

from pydantic import TypeAdapter

COMMON = {"start", "end", "pool", "lookback", "market_data", "benchmark", "batch_days", "cash", "commission", "tax"}
STAGES = {"model", "optimize", "control", "execution"}


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
        return {"backtest": built.model_dump(mode="json"), "params": parameter_type(algo).model_validate(built, from_attributes=True).model_dump(mode="json")}
    return {"params": form.model_validate(data["values"]).model_dump(mode="json")}


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
