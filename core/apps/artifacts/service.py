"""Local artifact registration and source-independent publication.

Callers own database transactions. Registered wheels have fresh UUID directories.
"""

import json
import re
import shutil
import tomllib
from contextlib import contextmanager
from copy import deepcopy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

import tomlkit
from packaging.utils import canonicalize_name
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion, utc_now
from .models import ResearchArtifact


class ArtifactError(ValueError):
    def __init__(self, reason: str, code: str = "invalid_artifact"):
        super().__init__(reason)
        self.reason, self.code = reason, code

    @property
    def detail(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def checked_path(path: Path | str, boundary: Path | None = None, *, directory: bool = False) -> Path:
    path = Path(path)
    root = Path(boundary or SoloSettings.SHARED_DIR).resolve()
    if not path.is_absolute():
        raise ArtifactError("Artifact paths must be absolute local shared paths")
    path = path.resolve()
    if path == root or not path.is_relative_to(root):
        raise ArtifactError("Artifact path is outside its owned shared directory")
    if not (path.is_dir() if directory else path.is_file()):
        raise ArtifactError("Artifact source is missing or is not a regular file")
    return path


def wheel_metadata(wheel: Path) -> tuple[str, str, list[str], str | None]:
    try:
        with ZipFile(wheel) as archive:
            metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise ArtifactError("Wheel must contain unique METADATA")
            metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
            if len(metadata.get_all("Name", [])) != 1 or len(metadata.get_all("Version", [])) != 1:
                raise ArtifactError("Wheel must declare one package name and version")
            package = canonicalize_name(metadata["Name"])
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package):
                raise ArtifactError("Wheel package identity is invalid")
            return package, metadata["Version"], metadata.get_all("Requires-Dist", []), metadata.get("Entry")
    except (BadZipFile, OSError, KeyError) as error:
        raise ArtifactError("Artifact is not a valid readable wheel") from error


def artifact_path(artifact: ResearchArtifact) -> Path:
    if (not re.fullmatch(r"[A-Za-z0-9_-]+", artifact.storage_directory)
            or Path(artifact.filename).name != artifact.filename or not artifact.filename.endswith(".whl")):
        raise ArtifactError("Registry artifact identity contains an unsafe path")
    return Path(SoloSettings.SHARED_DIR).absolute() / "artifacts" / artifact.storage_directory / artifact.filename


def validate_entry(package: str, entry: str) -> None:
    module = package.replace("-", "_")
    if not isinstance(entry, str) or not re.fullmatch(rf"{re.escape(module)}(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entry):
        raise ArtifactError("Artifact entry must belong to its explicit research package")


def artifact_snapshot(artifact: ResearchArtifact) -> dict:
    return {
        "id": str(artifact.id), "artifact_id": str(artifact.id), "kind": artifact.kind,
        "package": artifact.package_name, "version": artifact.version,
        "filename": artifact.filename, "size_bytes": artifact.size_bytes, "entry": artifact.entry,
        "scheme_version": artifact.scheme_version, "sources": deepcopy(artifact.sources),
        "origin_project_id": str(artifact.origin_project_id) if artifact.origin_project_id else None,
        "origin_project_name": artifact.origin_project_name,
        "origin_version_id": str(artifact.origin_version_id) if artifact.origin_version_id else None,
        "dependencies": deepcopy(artifact.dependencies or {}),
    }


def require_artifact(session: Session, snapshot: dict | UUID | str) -> ResearchArtifact:
    identifier = (snapshot.get("artifact_id") or snapshot.get("id")) if isinstance(snapshot, dict) else snapshot
    artifact = session.get(ResearchArtifact, UUID(str(identifier)))
    if artifact is None:
        raise ArtifactError("Accepted artifact no longer exists in the registry", "artifact_not_found")
    return artifact


def lock_registry(session: Session) -> None:
    """Serialize reference mutations with registry deletion on PostgreSQL."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(1936682081, 1)"))


@contextmanager
def artifact_directories(created: list[Path] | None = None):
    directories = []
    try:
        yield directories
    except Exception:
        for directory in reversed(directories):
            shutil.rmtree(directory)
        raise
    if created is not None:
        created.extend(directories)


def exact_artifacts(session: Session, identifiers, *, active: bool = True) -> dict[str, dict]:
    lock_registry(session)
    result = {}
    for identifier in identifiers:
        artifact = require_artifact(session, identifier)
        if active:
            require_active(artifact)
        previous = result.get(artifact.package_name)
        if previous and previous["id"] != str(artifact.id):
            raise ArtifactError(f"Artifacts select conflicting identities for {artifact.package_name}", "artifact_conflict")
        result[artifact.package_name] = artifact_snapshot(artifact)
    return result


def retirement_reason(artifact: ResearchArtifact) -> str | None:
    try:
        if artifact.sources:
            reason = policy.source_reason(artifact.sources)
            if reason:
                return reason
        elif artifact.kind != "dependency":
            return "Artifact has no authoritative release provenance"
        if artifact.scheme_version:
            return policy.release_reason("scheme", version=artifact.scheme_version)
        if artifact.kind != "dependency":
            return "Artifact has no exact frozen Scheme version"
    except policy.VersionPolicyError as error:
        return error.reason
    return None


def read_artifact(artifact: ResearchArtifact) -> dict:
    reason = retirement_reason(artifact)
    return {"id": str(artifact.id), "kind": artifact.kind, "package": artifact.package_name,
        "version": artifact.version, "filename": artifact.filename, "storageDirectory": artifact.storage_directory,
        "entry": artifact.entry, "schemeVersion": artifact.scheme_version, "sources": deepcopy(artifact.sources),
        "sourceProjectId": str(artifact.origin_project_id) if artifact.origin_project_id else None,
        "sourceProjectName": artifact.origin_project_name,
        "sourceVersionId": str(artifact.origin_version_id) if artifact.origin_version_id else None,
        "publishedAt": artifact.published_at.isoformat() if artifact.published_at else None,
        "retired": bool(reason), "retiredReason": reason, "sizeBytes": artifact.size_bytes,
        "dependencies": deepcopy(artifact.dependencies or {}), "bundlePath": artifact.bundle_path}


def require_active(artifact: ResearchArtifact) -> None:
    reason = retirement_reason(artifact)
    if reason:
        raise policy.VersionPolicyError(reason)


def register_file(session: Session, wheel: Path | str, project=None, *, component: dict | None = None,
                  scheme_version: str | None = None, created: list[Path] | None = None) -> ResearchArtifact:
    lock_registry(session)
    wheel = checked_path(wheel)
    if wheel.suffix != ".whl":
        raise ArtifactError("Only local wheel artifacts can be registered")
    component = component or {}
    package, version, requirements, metadata_entry = wheel_metadata(wheel)
    if any(key in component and component[key] != value for key, value in (("package", package), ("version", version))):
        raise ArtifactError("Wheel package/version differs from its component")
    research = policy.RESEARCH_PACKAGE.fullmatch(package)
    sources = deepcopy(policy.project_sources(project) if project is not None else component.get("sources", {}))
    origin_version = None
    if research:
        kind = research.group(1)
        if project is None or project.package_name != package or project.kind != kind:
            raise ArtifactError("Wheel package does not belong to the explicit source Project")
        entry = component.get("entry") or metadata_entry or f"{package.replace('-', '_')}:{'Factor' if kind == 'factor' else kind.title() + 'Algo'}"
        validate_entry(package, entry)
        runs = (Path(SoloSettings.SHARED_DIR) / "runs").resolve()
        if wheel.is_relative_to(runs):
            candidate = UUID(wheel.relative_to(runs).parts[0])
            saved = session.get(ProjectVersion, candidate)
            if saved is not None and saved.project_id == project.id:
                origin_version = candidate
        if scheme_version is None:
            scheme_version = str(policy.require_project(project))
        scheme = policy.formal_version(scheme_version)
        from core.apps.projects.templates import validate_template
        validate_template({"version": version, "dependencies": requirements}, scheme)
        policy.require_result_series(version, scheme)
        sources["scheme"]["version"] = scheme_version
        reason = policy.source_reason(sources)
        if reason:
            raise policy.VersionPolicyError(reason)
    else:
        kind, entry = "dependency", metadata_entry or component.get("entry") or ""
        if entry:
            validate_entry(package, entry)
    identifier = uuid4()
    artifact = ResearchArtifact(id=identifier, kind=kind, package_name=package, version=version,
        storage_directory=str(identifier), filename=wheel.name, size_bytes=wheel.stat().st_size, entry=entry,
        scheme_version=scheme_version, sources=sources,
        origin_project_id=project.id if project is not None else None,
        origin_project_name=project.name if project is not None else None, origin_version_id=origin_version,
        dependencies=deepcopy(getattr(project, "artifact_dependencies", None) or component.get("dependencies", {})))
    directory = artifact_path(artifact).parent
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    with artifact_directories(created) as directories:
        directories.append(directory)
        checked_path(directory, directory.parent, directory=True)
        shutil.copyfile(wheel, directory / artifact.filename)
        session.add(artifact)
        session.flush()
    return artifact


def register_asset(session: Session, wheel: Path | str, *, sources: dict | None = None,
                   scheme_version: str | None = None, project=None, component: dict | None = None,
                   created: list[Path] | None = None) -> ResearchArtifact:
    return register_file(session, wheel, project, component={**(component or {}), "sources": sources or {}},
                         scheme_version=scheme_version, created=created)


def _snapshot_ids(value):
    if isinstance(value, dict):
        for key in ("artifact_id", "id"):
            if isinstance(value.get(key), str):
                yield value[key]
                return
        for item in value.values():
            yield from _snapshot_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _snapshot_ids(item)


def _local_assets(directory: Path, lockfile: Path, lock: dict) -> dict[str, Path]:
    assets = {}
    for item in lock["package"]:
        source = item.get("source", {})
        if "path" in source or ("registry" in source and (
                Path(source["registry"]).is_absolute() or urlsplit(source["registry"]).scheme in {"", "file"})):
            wheel = policy.locked_local_wheel(directory, lockfile, item)
            name, version, _, _ = wheel_metadata(wheel)
            if name != canonicalize_name(item["name"]) or version != item["version"]:
                raise ArtifactError("Frozen local asset METADATA differs from its lock package")
            assets[name] = wheel
        elif {"directory", "editable"} & source.keys():
            raise ArtifactError("Published environments cannot depend on unfrozen source directories")
    return assets


def _bundle(artifact: ResearchArtifact, directory: Path, data: dict, lockfile: Path,
            lock: dict, snapshots: dict, assets: dict[str, Path], registered: dict[str, ResearchArtifact],
            created: list[Path]) -> Path:
    final = artifact_path(artifact).parent / "bundle"
    if final.exists():
        raise ArtifactError("Publication bundle already exists; refusing to overwrite it", "artifact_conflict")
    config = tomllib.loads(checked_path(lockfile.parent / "pyproject.toml", directory).read_text(encoding="utf-8"))
    destinations = {name: artifact_path(registered[name]) if name in registered else final / "wheels" / name / path.name
                    for name, path in assets.items()}
    uv = config.setdefault("tool", {}).setdefault("uv", {})
    sources = uv.setdefault("sources", {})
    # Only locked sources participate in publication; unused development sources do not.
    for name, locations in list(sources.items()):
        locations = locations if isinstance(locations, list) else [locations]
        if name not in destinations and any({"path", "directory", "editable"} & location.keys() for location in locations):
            del sources[name]
    for name, target in destinations.items():
        sources[name] = {"path": str(target)}
    derivative_lock = deepcopy(lock)
    for item in derivative_lock["package"]:
        name = canonicalize_name(item["name"])
        if name in destinations:
            item["source"] = {"path": str(destinations[name])}
            item.pop("sdist", None)
            # Preserve uv's existing wheel metadata; publication only rebases paths.
            item["wheels"] = [{**{key: value for key, value in wheel.items() if key not in {"path", "url"}},
                               "filename": destinations[name].name}
                              for wheel in item["wheels"]
                              if wheel.get("filename") == assets[name].name
                              or unquote(urlsplit(wheel.get("path", wheel.get("url", ""))).path).replace("\\", "/").rsplit("/", 1)[-1] == assets[name].name]
    def rebase_requirements(value):
        if isinstance(value, dict):
            name = canonicalize_name(value.get("name", ""))
            if name in destinations and any(key in value for key in ("path", "url", "index")):
                value.pop("url", None)
                value.pop("index", None)
                value["path"] = destinations[name].as_posix()
            for item in value.values():
                rebase_requirements(item)
        elif isinstance(value, list):
            for item in value:
                rebase_requirements(item)
    rebase_requirements(derivative_lock.get("manifest", {}))
    for item in derivative_lock["package"]:
        rebase_requirements(item.get("metadata", {}))
    derivative_input = deepcopy(data)
    derivative_input["environment"]["lockfile"] = str(final / "environment" / "uv.lock")
    derivative_input["output"] = str(final / "report")
    for component in policy.task_components(derivative_input).values():
        component["wheel"] = str(destinations[canonicalize_name(component["package"])])
    final.mkdir()
    created.append(final)
    for name, source in assets.items():
        if name not in registered:
            target = destinations[name]
            target.parent.mkdir(parents=True)
            shutil.copyfile(source, target)
    environment = final / "environment"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    (environment / "uv.lock").write_text(tomlkit.dumps(derivative_lock), encoding="utf-8")
    (final / "input.json").write_text(json.dumps(derivative_input, ensure_ascii=False, indent=2), encoding="utf-8")
    (final / "manifest.json").write_text(json.dumps({"artifact_id": str(artifact.id),
        "root_artifact": artifact_snapshot(artifact), "accepted_artifacts": snapshots},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return final


def verify_bundle(artifact: ResearchArtifact) -> tuple[Path, dict]:
    if not artifact.published_at or not artifact.bundle_path:
        raise ArtifactError("Published artifact has no independent bundle")
    root = checked_path(artifact.bundle_path, artifact_path(artifact).parent, directory=True)
    data = json.loads(checked_path(root / "input.json", root).read_text(encoding="utf-8"))
    return root, data


def publish_version(session: Session, project: Project, version: ProjectVersion, *,
                    created: list[Path] | None = None) -> ResearchArtifact:
    lock_registry(session)
    if version.project_id != project.id or version.status != "success":
        raise ArtifactError("Only the owning Project's successful saved result can be published")
    snapshots = policy.snapshot_values(version.accepted_artifacts)
    if project.package_name not in snapshots:
        raise ArtifactError("Saved result has no accepted own-root artifact")
    artifact = require_artifact(session, snapshots[project.package_name])
    require_active(artifact)
    if (artifact.kind != project.kind or artifact.version != version.package_version
            or artifact.origin_project_id != project.id):
        raise ArtifactError("Accepted artifact does not match the saved result")
    if artifact.published_at:
        return artifact
    if session.scalar(select(ResearchArtifact.id).where(ResearchArtifact.package_name == artifact.package_name,
            ResearchArtifact.version == artifact.version, ResearchArtifact.published_at.is_not(None))):
        raise ArtifactError("An artifact is already published for this package/version", "artifact_conflict")
    directory = policy.run_path(version.id)
    data, lockfile, lock = policy.task_files(directory, project.kind)
    actual = policy.require_locked_scheme(lock)
    if artifact.scheme_version != str(actual) or version.scheme_version != str(actual):
        raise ArtifactError("Publication ActualScheme differs from the saved result")
    report = json.loads(checked_path(directory / "report" / "run.json", directory).read_text(encoding="utf-8"))
    if report.get("status") != "success" or report.get("versions", {}).get("scheme") != str(actual):
        raise ArtifactError("Publication requires a successful report with the saved ActualScheme")
    closure = exact_artifacts(session, snapshots.values())
    registered = {name: session.get(ResearchArtifact, UUID(snapshot["id"])) for name, snapshot in closure.items()}
    assets = _local_assets(directory, lockfile, lock)
    with artifact_directories(created) as directories:
        for name, wheel in assets.items():
            if name not in closure:
                if policy.RESEARCH_PACKAGE.fullmatch(name):
                    raise ArtifactError(f"Research dependency {name} has no accepted artifact")
                dependency = register_asset(session, wheel, sources=artifact.sources, scheme_version=str(actual),
                                            created=directories)
                closure[name] = artifact_snapshot(dependency)
        artifact.dependencies = {name: snapshot for name, snapshot in closure.items() if name != artifact.package_name}
        bundle = _bundle(artifact, directory, data, lockfile, lock, closure, assets, registered, directories)
        artifact.bundle_path, artifact.published_at = str(bundle), utc_now()
        session.flush()
    return artifact


def publish_version_committed(session: Session, project: Project, version: ProjectVersion) -> ResearchArtifact:
    with artifact_directories() as directories:
        artifact = publish_version(session, project, version, created=directories)
        session.commit()
    return artifact


def artifact_component(session: Session, identifier: UUID | str) -> dict:
    artifact = require_artifact(session, identifier)
    require_active(artifact)
    if artifact.published_at is None or artifact.kind == "dependency":
        raise ArtifactError("Strategy selection requires a published research artifact")
    root, data = verify_bundle(artifact)
    component = deepcopy(data["factor"] if artifact.kind == "factor" else data["algos"][artifact.kind])
    component["wheel"] = str(checked_path(artifact_path(artifact)))
    snapshot = artifact_snapshot(artifact)
    return {"label": f"{artifact.origin_project_name or artifact.package_name} · {artifact.version}",
        "kind": artifact.kind, "artifact_id": str(artifact.id), "version_id": None,
        "project_id": str(artifact.origin_project_id) if artifact.origin_project_id else None,
        "scheme_version": artifact.scheme_version, "root_sources": deepcopy(artifact.sources),
        "component": component, "component_snapshot": {key: snapshot[key] for key in ("package", "version", "entry")},
        "input": data, "environment_path": str(root / "environment"), "published": True,
        "dependencies": deepcopy(artifact.dependencies),
        "accepted_artifacts": {artifact.package_name: snapshot, **deepcopy(artifact.dependencies)}}


def gc_unreferenced(session: Session) -> dict[str, int]:
    """Remove unreferenced unpublished artifacts without consulting descriptive origins."""
    from core.apps.strategies.models import Strategy
    lock_registry(session)
    artifacts = list(session.scalars(select(ResearchArtifact).with_for_update()))
    by_id = {str(item.id): item for item in artifacts}
    retained = {str(item.id) for item in artifacts if item.published_at}
    for project in session.scalars(select(Project)):
        retained.update(_snapshot_ids(project.artifact_dependencies))
    for record in list(session.scalars(select(ProjectVersion))) + list(session.scalars(select(Strategy))):
        retained.update(_snapshot_ids(record.accepted_artifacts))
    pending = list(retained)
    while pending:
        artifact = by_id.get(pending.pop())
        if artifact is None:
            continue
        for identifier in _snapshot_ids(artifact.dependencies):
            if identifier not in retained:
                retained.add(identifier)
                pending.append(identifier)
    removed = 0
    for artifact in artifacts:
        if str(artifact.id) not in retained:
            directory = artifact_path(artifact).parent
            if directory.exists():
                checked_path(directory, directory.parent, directory=True)
                shutil.rmtree(directory)
            session.delete(artifact)
            removed += 1
    session.flush()
    return {"removed": removed, "retained": len(artifacts) - removed}
