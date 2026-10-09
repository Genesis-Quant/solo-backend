"""Local, immutable artifact registration and source-independent publication.

Nothing here executes a wheel, changes an accepted run, or resolves new versions.
Callers own the database transaction; filesystem writes are digest addressed.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from copy import deepcopy
from datetime import UTC, timedelta
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zipfile import BadZipFile, ZipFile

import tomlkit
from packaging.utils import canonicalize_name
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion, utc_now
from .models import ArtifactInstallation, ResearchArtifact

INSTALLATION_TTL = timedelta(minutes=30)


class ArtifactError(ValueError):
    def __init__(self, reason: str, code: str = "invalid_artifact"):
        super().__init__(reason)
        self.reason, self.code = reason, code

    @property
    def detail(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def checked_path(path: Path | str, boundary: Path | None = None, *, directory: bool = False) -> Path:
    """Reject links as well as lexical/resolved escapes; only shared local files."""
    path = Path(path)
    root = Path(boundary or SoloSettings.SHARED_DIR).absolute()
    if not path.is_absolute():
        raise ArtifactError("Artifact paths must be absolute local shared paths")
    path = path.absolute()
    if path == root or not path.is_relative_to(root):
        raise ArtifactError("Artifact path is outside its owned shared directory")
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise ArtifactError("Artifact paths cannot contain links")
        current = current.parent
    if root.is_symlink() or root.is_junction() or not path.resolve().is_relative_to(root.resolve()):
        raise ArtifactError("Artifact path redirects outside its shared directory")
    if not (path.is_dir() if directory else path.is_file()):
        raise ArtifactError("Artifact source is missing or is not a regular file")
    return path.resolve()


def wheel_metadata(wheel: Path) -> tuple[str, str, list[str], set[str]]:
    try:
        with ZipFile(wheel) as archive:
            members = archive.namelist()
            metadata_files = [name for name in members if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1 or len(members) != len(set(members)):
                raise ArtifactError("Wheel must contain unique METADATA and archive entries")
            if archive.getinfo(metadata_files[0]).file_size > 2 * 1024 * 1024:
                raise ArtifactError("Wheel METADATA exceeds the supported size")
            metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
            if len(metadata.get_all("Name", [])) != 1 or len(metadata.get_all("Version", [])) != 1:
                raise ArtifactError("Wheel must declare one package name and version")
            package = canonicalize_name(metadata["Name"])
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package):
                raise ArtifactError("Wheel package identity is invalid")
            return package, metadata["Version"], metadata.get_all("Requires-Dist", []), set(members)
    except (BadZipFile, OSError, KeyError) as error:
        raise ArtifactError("Artifact is not a valid readable wheel") from error


def artifact_path(artifact: ResearchArtifact) -> Path:
    if (not re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
            or Path(artifact.filename).name != artifact.filename or not artifact.filename.endswith(".whl")):
        raise ArtifactError("Registry artifact identity contains an unsafe path")
    return Path(SoloSettings.SHARED_DIR).absolute() / "artifacts" / artifact.sha256 / artifact.filename


def _check_bytes(artifact: ResearchArtifact, wheel: Path) -> None:
    content = wheel.read_bytes()
    if len(content) != artifact.size_bytes or hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise ArtifactError("Artifact wheel sha256 or size no longer matches the registry", "artifact_tampered")
    package, version, _, names = wheel_metadata(wheel)
    if package != artifact.package_name or version != artifact.version:
        raise ArtifactError("Artifact METADATA no longer matches the registry", "artifact_tampered")
    if artifact.entry:
        _validate_entry(package, artifact.entry, names)


def _validate_entry(package: str, entry: str, names: set[str]) -> None:
    module = package.replace("-", "_")
    if not isinstance(entry, str) or not re.fullmatch(rf"{re.escape(module)}(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entry):
        raise ArtifactError("Artifact entry must belong to its explicit research package")
    entry_module = entry.split(":", 1)[0].replace(".", "/")
    if not {entry_module + ".py", entry_module + "/__init__.py"} & names:
        raise ArtifactError("Wheel does not contain its declared entry module")


def artifact_snapshot(artifact: ResearchArtifact) -> dict:
    return {
        "id": str(artifact.id), "artifact_id": str(artifact.id), "kind": artifact.kind,
        "package": artifact.package_name, "version": artifact.version, "sha256": artifact.sha256,
        "filename": artifact.filename, "size_bytes": artifact.size_bytes, "entry": artifact.entry,
        "scheme_version": artifact.scheme_version, "sources": deepcopy(artifact.sources),
        "origin_project_id": str(artifact.origin_project_id) if artifact.origin_project_id else None,
        "origin_project_name": artifact.origin_project_name,
        "origin_version_id": str(artifact.origin_version_id) if artifact.origin_version_id else None,
        "dependencies": deepcopy(artifact.dependencies or {}),
    }


def require_artifact(session: Session, snapshot: dict | UUID | str, *, wheel: Path | str | None = None,
                     path: Path | str | None = None) -> ResearchArtifact:
    """Validate a frozen snapshot and registry bytes, optionally a task/consumer copy."""
    identifier = snapshot.get("artifact_id") or snapshot.get("id") if isinstance(snapshot, dict) else snapshot
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError, AttributeError) as error:
        raise ArtifactError("Accepted artifact snapshot is missing a valid artifact ID") from error
    artifact = session.get(ResearchArtifact, identifier)
    if artifact is None:
        raise ArtifactError("Accepted artifact no longer exists in the registry", "artifact_not_found")
    if isinstance(snapshot, dict):
        expected = artifact_snapshot(artifact)
        for key in ("kind", "package", "version", "sha256", "entry", "sources"):
            if snapshot.get(key) != expected[key]:
                raise ArtifactError(f"Accepted artifact {key} differs from its immutable registry snapshot", "artifact_tampered")
        if snapshot.get("scheme_version", snapshot.get("schemeVersion")) != artifact.scheme_version:
            raise ArtifactError("Accepted artifact Scheme differs from its immutable registry snapshot", "artifact_tampered")
        for key in ("origin_project_id", "origin_project_name", "origin_version_id", "filename", "size_bytes"):
            if key in snapshot and snapshot[key] != expected[key]:
                raise ArtifactError(f"Accepted artifact {key} differs from its immutable origin", "artifact_tampered")
    _check_bytes(artifact, checked_path(artifact_path(artifact)))
    if wheel is not None or path is not None:
        _check_bytes(artifact, checked_path(wheel if wheel is not None else path))
    return artifact


def lock_registry(session: Session) -> None:
    """Serialize short registry/ref mutations with deletion GC on PostgreSQL."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(1936682081, 1)"))


def exact_artifacts(session: Session, identifiers, *, active: bool = True) -> dict[str, dict]:
    """Validate just the submitted objects, not their publication dependency graph."""
    lock_registry(session)
    result = {}
    for item in identifiers:
        identifier = (item.get("artifact_id") or item.get("id")) if isinstance(item, dict) else item
        # Serialize installing/accepting these references with registry GC. Once
        # this transaction commits, either pending or accepted refs protect them.
        artifact = session.scalar(select(ResearchArtifact).where(
            ResearchArtifact.id == UUID(str(identifier))).with_for_update().execution_options(populate_existing=True))
        if artifact is None:
            raise ArtifactError("Accepted artifact no longer exists in the registry", "artifact_not_found")
        require_artifact(session, item)
        if active:
            require_active(artifact)
        old = result.get(artifact.package_name)
        if old and old["id"] != str(artifact.id):
            raise ArtifactError(f"Installed artifacts select conflicting bytes for {artifact.package_name}", "artifact_conflict")
        result[artifact.package_name] = artifact_snapshot(artifact)
    return result


def _expires_at(installation: ArtifactInstallation):
    # SQLite returns timezone-naive DateTime despite timezone=True.
    value = installation.expires_at
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def require_installation(session: Session, consumer_id: UUID, install_id: UUID, *,
                         allow_expired: bool = False) -> ArtifactInstallation:
    installation = session.scalar(select(ArtifactInstallation).where(
        ArtifactInstallation.id == install_id).with_for_update().execution_options(populate_existing=True))
    if installation is None or installation.consumer_project_id != consumer_id:
        raise ArtifactError("Pending installation does not belong to this consumer", "artifact_conflict")
    if not allow_expired and _expires_at(installation) <= utc_now():
        raise ArtifactError("Pending installation expired; restore the previous environment before retrying", "artifact_conflict")
    return installation


def installation_references_live(installation: ArtifactInstallation, now=None) -> bool:
    # An accepted receipt cannot distinguish a lost complete reply from a lost abort.
    return ((installation.artifacts or {}).get("accepted") is not None
            or _expires_at(installation) > (now or utc_now()))


def require_settled_installations(session: Session, consumer_id: UUID, install_id: UUID | None = None) -> None:
    for installation in session.scalars(select(ArtifactInstallation).where(
            ArtifactInstallation.consumer_project_id == consumer_id).with_for_update()
            .execution_options(populate_existing=True)):
        if installation.id != install_id and (installation.artifacts or {}).get("accepted") is not None:
            raise ArtifactError("A previous installation requires completion or rollback before changing dependencies", "artifact_conflict")


def protect_installation(session: Session, consumer: Project, install_id: UUID, identifiers=()) -> ArtifactInstallation:
    """Begin/renew a consumer transaction and add exact pending references."""
    lock_registry(session)
    require_settled_installations(session, consumer.id, install_id)
    installation = session.scalar(select(ArtifactInstallation).where(
        ArtifactInstallation.id == install_id).with_for_update().execution_options(populate_existing=True))
    if installation is None:
        installation = ArtifactInstallation(id=install_id, consumer_project_id=consumer.id,
            artifacts={"format": 1, "before": deepcopy(consumer.artifact_dependencies or {}),
                       "pending": {}, "accepted": None}, expires_at=utc_now() + INSTALLATION_TTL)
        session.add(installation)
    else:
        installation = require_installation(session, consumer.id, install_id)
    receipt = deepcopy(installation.artifacts)
    if receipt["accepted"] is not None:
        raise ArtifactError("Accepted installation is awaiting completion or rollback", "artifact_conflict")
    # IDs (not packages) allow an upgrade to protect old and new bytes together.
    for identifier in identifiers:
        artifact = session.scalar(select(ResearchArtifact).where(
            ResearchArtifact.id == UUID(str(identifier))).with_for_update().execution_options(populate_existing=True))
        if artifact is None:
            raise ArtifactError("Pending artifact no longer exists in the registry", "artifact_not_found")
        require_artifact(session, artifact.id)
        require_active(artifact)
        receipt["pending"][str(artifact.id)] = artifact_snapshot(artifact)
    if len(receipt["pending"]) > 2048:
        raise ArtifactError("Pending installation exceeds the supported artifact count")
    installation.artifacts = receipt
    installation.expires_at = utc_now() + INSTALLATION_TTL
    session.flush()
    return installation


def read_installation(installation: ArtifactInstallation) -> dict:
    return {"install_id": str(installation.id), "expires_at": _expires_at(installation).isoformat(),
        "artifacts": list(installation.artifacts["pending"].values())}


def retirement_reason(artifact: ResearchArtifact) -> str | None:
    try:
        if artifact.sources:
            reason = policy.source_reason(artifact.sources)
            if reason:
                return reason
        elif artifact.kind != "dependency":
            raise ArtifactError("Artifact has no authoritative release provenance")
        if artifact.scheme_version:
            return policy.release_reason("scheme", version=artifact.scheme_version)
        if artifact.kind != "dependency":
            return "Artifact has no exact frozen Scheme version"
    except (ArtifactError, policy.VersionPolicyError, ValueError) as error:
        return str(error)
    return None


def read_artifact(artifact: ResearchArtifact) -> dict:
    reason = retirement_reason(artifact)
    return {"id": str(artifact.id), "kind": artifact.kind, "package": artifact.package_name,
        "version": artifact.version, "sha256": artifact.sha256, "filename": artifact.filename,
        "entry": artifact.entry, "schemeVersion": artifact.scheme_version, "sources": deepcopy(artifact.sources),
        "sourceProjectId": str(artifact.origin_project_id) if artifact.origin_project_id else None,
        "sourceProjectName": artifact.origin_project_name,
        "sourceVersionId": str(artifact.origin_version_id) if artifact.origin_version_id else None,
        "publishedAt": artifact.published_at.isoformat() if artifact.published_at else None,
        "retired": bool(reason), "retiredReason": reason, "sizeBytes": artifact.size_bytes,
        "dependencies": deepcopy(artifact.dependencies or {}),
        "bundlePath": artifact.bundle_path, "bundleManifestSha256": artifact.bundle_manifest_sha256}


def require_active(artifact: ResearchArtifact) -> None:
    reason = retirement_reason(artifact)
    if reason:
        raise policy.VersionPolicyError(reason)


def authoritative_source_wheel(session: Session, project: Project, *, package: str, version: str,
                               sha256: str, historical: bool = False) -> tuple[Path, str]:
    """Copied unknown research wheels need exact original-owned byte evidence."""
    saved_versions = session.scalars(select(ProjectVersion).where(ProjectVersion.project_id == project.id,
        ProjectVersion.package_version == version).order_by(ProjectVersion.created_at.desc()))
    for saved in saved_versions:
        # Upstream frozen tasks are authoritative only when originally accepted.
        if not historical and (not saved.input_sha256 or not saved.lock_sha256):
            continue
        directory = policy.run_path(saved.id).absolute()
        try:
            data, lockfile, lock = policy.task_files(directory, project.kind,
                input_sha256=saved.input_sha256, lock_sha256=saved.lock_sha256)
            component = policy.task_components(data).get(package)
            if not component or component.get("version") != version or component.get("sha256") != sha256:
                continue
            wheel = checked_path(policy.file_in_run(directory, component["wheel"]), directory)
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != sha256:
                continue
            locked = policy.local_task_packages(lock).get(package)
            if (locked is None or locked["version"] != version
                    or checked_path(policy.locked_local_wheel(directory, lockfile, locked), directory) != wheel):
                continue
            name, metadata_version, requirements, _ = wheel_metadata(wheel)
            actual, _ = policy.locked_scheme(lock)
            if name != package or metadata_version != version or saved.scheme_version != str(actual):
                continue
            from core.apps.projects.templates import validate_template
            validate_template({"version": version, "dependencies": requirements}, actual)
            return wheel, str(actual)
        except (ArtifactError, OSError, ValueError, KeyError, TypeError):
            continue
    directory = policy.project_path(project).absolute()
    try:
        directory = checked_path(directory, directory=True)
        owner = json.loads(checked_path(directory / ".solo", directory).read_text(encoding="utf-8"))
        if any(owner.get(key) != value for key, value in {
            "project_id": str(project.id), "kind": project.kind, "name": project.name,
        }.items()):
            raise ArtifactError("Original source workspace no longer belongs to its Project")
        lockfile = checked_path(directory / "uv.lock", directory)
        lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
        actual = policy.locked_scheme(lock)[0] if historical else policy.require_project(project, lock=lock)
        configfile = checked_path(directory / "pyproject.toml", directory)
        config = tomllib.loads(configfile.read_text(encoding="utf-8"))["project"]
        if canonicalize_name(config["name"]) != package:
            raise ArtifactError("Original source configuration has a different research identity")
        from core.apps.projects.templates import validate_template
        validate_template(config, actual)
        # Inspect only actual source-owned wheels, never a consumer run and never
        # a copied .venv/link. A live source lock supplies its independent Scheme.
        for wheel in directory.rglob("*.whl"):
            if {".venv", ".git"} & set(wheel.relative_to(directory).parts):
                continue
            wheel = checked_path(wheel, directory)
            if hashlib.sha256(wheel.read_bytes()).hexdigest() == sha256:
                name, candidate_version, _, _ = wheel_metadata(wheel)
                if name == package and candidate_version == version:
                    return wheel, str(actual)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ArtifactError("Unknown copied research wheel has no authoritative original source evidence") from error
    raise ArtifactError("Unknown copied research wheel has no exact original-owned wheel evidence")


def _authoritative_sources(session: Session, project, scheme_version: str | None, wheel: Path) -> tuple[dict, UUID | None]:
    sources = deepcopy(policy.project_sources(project))
    version_id = None
    runs = Path(SoloSettings.SHARED_DIR).absolute() / "runs"
    if wheel.is_relative_to(runs):
        try:
            candidate_id = UUID(wheel.relative_to(runs).parts[0])
        except (ValueError, IndexError):
            candidate_id = None
        saved = session.get(ProjectVersion, candidate_id) if candidate_id else None
        # A source dependency inside a consumer's run is NOT source provenance.
        if saved is not None and saved.project_id == project.id:
            run = runs / str(candidate_id)
            data, lockfile, lock = policy.task_files(run, project.kind,
                input_sha256=saved.input_sha256, lock_sha256=saved.lock_sha256)
            component = policy.task_components(data).get(project.package_name)
            if component is None or checked_path(policy.file_in_run(run, component["wheel"]), run) != wheel:
                raise ArtifactError("Original owned run does not bind this exact research wheel")
            if component.get("sha256") != hashlib.sha256(wheel.read_bytes()).hexdigest():
                raise ArtifactError("Original owned run wheel differs from its accepted component", "artifact_tampered")
            locked = policy.local_task_packages(lock).get(project.package_name)
            if (locked is None or locked["version"] != component["version"]
                    or checked_path(policy.locked_local_wheel(run, lockfile, locked), run) != wheel):
                raise ArtifactError("Original owned run does not lock this exact root wheel")
            actual, commit = policy.locked_scheme(lock)
            if saved.scheme_version is not None and saved.scheme_version != str(actual):
                raise ArtifactError("Original saved Scheme differs from its exact owned run lock")
            if scheme_version and str(actual) != scheme_version:
                raise ArtifactError("Registration Scheme differs from the actual original-owned run lock")
            scheme_version, version_id = str(actual), candidate_id
            if commit:
                sources["scheme"]["commit"] = commit
    workspace = policy.project_path(project).absolute()
    if version_id is None:
        # Fresh consumer-owned producer wheels still derive the commit from the
        # original live source. Never reuse the destination's run/lock commit.
        lockfile = checked_path(workspace / "uv.lock", workspace)
        actual, commit = policy.locked_scheme(tomllib.loads(lockfile.read_text(encoding="utf-8")))
        if scheme_version and str(actual) != scheme_version:
            raise ArtifactError("Registration Scheme differs from the actual original source lock")
        scheme_version = str(actual)
        if commit:
            sources["scheme"]["commit"] = commit
    if scheme_version:
        sources["scheme"]["version"] = scheme_version
    return sources, version_id


def _store_wheel(wheel: Path, digest: str, filename: str) -> None:
    root = Path(SoloSettings.SHARED_DIR).absolute() / "artifacts"
    root.mkdir(exist_ok=True)
    if root.is_symlink() or root.is_junction():
        raise ArtifactError("Artifact registry cannot be a link")
    directory = root / digest
    directory.mkdir(exist_ok=True)
    checked_path(directory, root, directory=True)
    target = directory / filename
    if target.exists():
        content = checked_path(target, root).read_bytes()
        if content != wheel.read_bytes():
            raise ArtifactError("Digest-addressed wheel already contains different bytes", "artifact_tampered")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".wheel-", dir=directory)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        shutil.copyfile(wheel, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise ArtifactError("Wheel changed while registration was copying it", "artifact_tampered")
        # Hard-link creation is atomic and refuses to replace an existing object.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if checked_path(target, root).read_bytes() != temporary.read_bytes():
                raise ArtifactError("Concurrent registration supplied different bytes", "artifact_tampered")
    finally:
        temporary.unlink(missing_ok=True)


def register_file(session: Session, wheel: Path | str, project=None, *, component: dict | None = None,
                  scheme_version: str | None = None, historical: bool = False) -> ResearchArtifact:
    lock_registry(session)
    wheel = checked_path(wheel)
    if wheel.suffix != ".whl":
        raise ArtifactError("Only local wheel artifacts can be registered")
    component = component or {}
    package, version, requirements, names = wheel_metadata(wheel)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    for key, actual in (("package", package), ("version", version), ("sha256", digest)):
        if key in component and component[key] is not None:
            expected = canonicalize_name(component[key]) if key == "package" else component[key]
            if expected != actual:
                raise ArtifactError(f"Wheel {key} does not match the authoritative component", "artifact_tampered")
    existing = session.scalar(select(ResearchArtifact).where(ResearchArtifact.sha256 == digest))
    research = policy.RESEARCH_PACKAGE.fullmatch(package)
    if research:
        kind = research.group(1)
        expected_package = getattr(project, "package_name", None) or (
            f"{project.kind}-{project.id.hex[:4]}" if project is not None else None
        )
        if project is not None and (expected_package != package or project.kind != kind):
            raise ArtifactError("Wheel package does not belong to the explicit source Project")
        if project is None and existing is None:
            raise ArtifactError("Research artifacts require an authoritative original Project owner")
        if existing is not None:
            entry = component.get("entry") or existing.entry
        else:
            entry = component.get("entry") or f"{package.replace('-', '_')}:{'Factor' if kind == 'factor' else kind.title() + 'Algo'}"
        _validate_entry(package, entry, names)
    else:
        kind, entry = "dependency", component.get("entry") or ""
        if entry:
            raise ArtifactError("Runtime dependency assets cannot claim a research entry")
    if existing is not None:
        if (existing.package_name, existing.version, existing.entry, existing.kind) != (package, version, entry, kind):
            raise ArtifactError("An existing wheel digest has a conflicting entry or identity", "artifact_conflict")
        if scheme_version and existing.scheme_version and scheme_version != existing.scheme_version:
            raise ArtifactError("An existing wheel digest has a conflicting frozen Scheme", "artifact_conflict")
        if research and project is not None and existing.origin_project_id != project.id:
            raise ArtifactError("An existing research digest belongs to a different original Project")
        require_artifact(session, existing.id, wheel=wheel)
        if not historical:
            require_active(existing)
        return existing
    if research:
        provenance_wheel = wheel
        if scheme_version is None:
            # Caller without explicit producer context is an unknown copied
            # upstream wheel. Resolve original byte evidence, not consumerActual.
            provenance_wheel, scheme_version = authoritative_source_wheel(session, project,
                package=package, version=version, sha256=digest, historical=historical)
        sources, origin_version = _authoritative_sources(session, project, scheme_version, provenance_wheel)
        scheme_version = sources["scheme"].get("version")
        scheme = policy.formal_version(scheme_version)
        actual = policy.formal_version(version)
        if (actual.major, actual.minor) != (scheme.major, scheme.minor):
            raise ArtifactError("Research artifact must use the frozen Scheme major/minor")
        if not historical:
            from core.apps.projects.templates import validate_template
            validate_template({"version": version, "dependencies": requirements}, scheme)
            reason = policy.source_reason(sources) or policy.release_reason("scheme", version=scheme_version)
            if reason:
                raise policy.VersionPolicyError(reason)
        # Only a real owned run is a descriptive saved-version origin.
        saved = session.get(ProjectVersion, origin_version) if origin_version else None
        if saved is None or saved.project_id != project.id:
            origin_version = None
    else:
        sources = deepcopy(component.get("sources") or (policy.project_sources(project) if project is not None else {}))
        if scheme_version and sources.get("scheme"):
            sources["scheme"]["version"] = scheme_version
        origin_version = None
    dependencies = deepcopy(component.get("dependencies") or getattr(project, "artifact_dependencies", None) or {})
    dependencies.pop(package, None)
    artifact = ResearchArtifact(id=UUID(bytes=os.urandom(16)), kind=kind, package_name=package, version=version,
        sha256=digest, filename=wheel.name, size_bytes=wheel.stat().st_size, entry=entry,
        scheme_version=scheme_version, sources=sources,
        origin_project_id=project.id if project is not None else None,
        origin_project_name=getattr(project, "name", None), origin_version_id=origin_version,
        dependencies=dependencies)
    _store_wheel(wheel, digest, artifact.filename)
    session.add(artifact)
    session.flush()
    return artifact


def register_asset(session: Session, wheel: Path | str, *, sources: dict | None = None,
                   scheme_version: str | None = None, project=None, component: dict | None = None,
                   historical: bool = False) -> ResearchArtifact:
    component = {**(component or {})}
    if sources is not None:
        component["sources"] = sources
    return register_file(session, wheel, project, component=component,
                         scheme_version=scheme_version, historical=historical)


def _snapshot_values(value):
    if isinstance(value, dict):
        if value.get("artifact_id") or value.get("id"):
            yield value
        else:
            for item in value.values():
                yield from _snapshot_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _snapshot_values(item)


def artifact_closure(session: Session, artifacts) -> dict[str, dict]:
    """Resolve and validate every accepted registry dependency, rejecting ambiguity."""
    result, visited = {}, set()
    pending = list(artifacts)
    while pending:
        item = pending.pop()
        artifact = require_artifact(session, item)
        require_active(artifact)
        snapshot = artifact_snapshot(artifact)
        old = result.get(artifact.package_name)
        if old and old["sha256"] != artifact.sha256:
            raise ArtifactError(f"Artifact closure selects conflicting bytes for {artifact.package_name}", "artifact_conflict")
        result[artifact.package_name] = snapshot
        if artifact.id not in visited:
            visited.add(artifact.id)
            pending.extend(_snapshot_values(artifact.dependencies))
    return result


def _local_assets(directory: Path, lockfile: Path, lock: dict) -> dict[str, Path]:
    result = {}
    for item in lock["package"]:
        source = item.get("source", {})
        if "path" in source or ("registry" in source and (
                Path(source["registry"]).is_absolute() or urlsplit(source["registry"]).scheme in {"", "file"})):
            wheel = policy.locked_local_wheel(directory, lockfile, item)
            checked_path(wheel, directory)
            name, version, _, _ = wheel_metadata(wheel)
            if name != canonicalize_name(item["name"]) or version != item["version"]:
                raise ArtifactError("Frozen local asset METADATA differs from its selected lock package")
            result[name] = wheel
        elif {"directory", "editable"} & source.keys():
            raise ArtifactError("Published environments cannot depend on unfrozen source directories")
    return result


def _copy_file(source: Path, target: Path, boundary: Path) -> None:
    checked_path(source, boundary)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _bundle_inventory(directory: Path) -> tuple[dict[str, str], set[str]]:
    """Inventory an exact bundle tree without following links or special files."""
    checked_path(directory, directory.parent, directory=True)
    files, directories, pending = {}, set(), [directory]
    while pending:
        for child in pending.pop().iterdir():
            if child.is_symlink() or child.is_junction():
                raise ArtifactError("Publication recovery cannot contain links", "artifact_tampered")
            relative = child.relative_to(directory).as_posix()
            if child.is_dir():
                checked_path(child, directory, directory=True)
                directories.add(relative)
                pending.append(child)
            else:
                files[relative] = hashlib.sha256(checked_path(child, directory).read_bytes()).hexdigest()
    return files, directories


def _discard_bundle_stage(stage: Path, artifact: ResearchArtifact) -> None:
    """Only clean the UUID-owned temporary directory created by this invocation."""
    if (stage.parent != artifact_path(artifact).parent
            or not re.fullmatch(rf"\.bundle-{artifact.id}-[a-z0-9_]+", stage.name)):
        raise ArtifactError("Publication stage does not belong to this exact artifact", "artifact_tampered")
    if not stage.exists() and not stage.is_symlink() and not stage.is_junction():
        return
    files, _ = _bundle_inventory(stage)
    if "manifest.json" in files:
        manifest = json.loads(checked_path(stage / "manifest.json", stage).read_bytes())
        if not isinstance(manifest, dict) or manifest.get("artifact_id") != str(artifact.id):
            raise ArtifactError("Publication stage belongs to a different artifact", "artifact_tampered")
    shutil.rmtree(stage)


def _sync_bundle_directory(directory: Path) -> None:
    # Windows cannot fsync directory handles; every bundle file is still fsynced.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _bundle(session: Session, artifact: ResearchArtifact, directory: Path, data: dict, lockfile: Path,
            lock: dict, snapshots: dict) -> Path:
    final = artifact_path(artifact).parent / "bundle"
    checked_path(final.parent, Path(SoloSettings.SHARED_DIR).absolute() / "artifacts", directory=True)
    assets = _local_assets(directory, lockfile, lock)
    if artifact.package_name not in assets:
        raise ArtifactError("Published root is not the exact locally locked wheel")
    environment_config = checked_path(lockfile.parent / "pyproject.toml", directory)
    # A deterministic complete tree is the durable recovery intent. On retry,
    # compare it with a fresh reconstruction from DB-accepted bytes before
    # adopting; never infer ownership from a directory name or on-disk hashes.
    stage = Path(tempfile.mkdtemp(prefix=f".bundle-{artifact.id}-", dir=final.parent))
    destinations = {name: final / "wheels" / name / path.name for name, path in assets.items()}
    try:
        for name, source in assets.items():
            target = stage / destinations[name].relative_to(final)
            _copy_file(source, target, directory)
        _copy_file(directory / "input.json", stage / "original" / "input.json", directory)
        _copy_file(lockfile, stage / "original" / "uv.lock", directory)
        _copy_file(environment_config, stage / "original" / "pyproject.toml", directory)
        config = tomllib.loads(environment_config.read_text(encoding="utf-8"))
        uv = config.setdefault("tool", {}).setdefault("uv", {})
        sources = uv.setdefault("sources", {})
        for name, target in destinations.items():
            sources[name] = {"path": str(target)}
        for name, source in list(sources.items()):
            locations = source if isinstance(source, list) else [source]
            if any(isinstance(location, dict) and {"path", "directory", "editable"} & location.keys() for location in locations) and name not in destinations:
                raise ArtifactError(f"Environment source {name} has no exact locally locked artifact")
        if "index" in uv:
            uv["index"] = [item for item in uv["index"] if item.get("format") != "flat"]
        derivative_lock = deepcopy(lock)
        for item in derivative_lock["package"]:
            name = canonicalize_name(item["name"])
            if name in destinations:
                item["source"] = {"path": str(destinations[name])}
                item.pop("sdist", None)
                item["wheels"] = [{"filename": assets[name].name,
                    "hash": "sha256:" + hashlib.sha256(assets[name].read_bytes()).hexdigest(),
                    "size": assets[name].stat().st_size}]
        # uv's manifest/metadata repeats exact sources. Rebase those too, not
        # remote URLs or version pins; private index policy stays untouched.
        replacements = {str(directory): str(final), directory.as_posix(): final.as_posix(),
                        directory.as_uri(): final.as_uri()}
        for name, source in assets.items():
            target = destinations[name]
            replacements.update({str(source): str(target), source.as_posix(): target.as_posix(), source.as_uri(): target.as_uri()})
        def rebase(value):
            if isinstance(value, str):
                for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
                    value = value.replace(old, new)
                return value
            if isinstance(value, list):
                return [rebase(item) for item in value]
            if isinstance(value, dict):
                return {key: rebase(item) for key, item in value.items()}
            return value
        derivative_lock = rebase(derivative_lock)
        # uv repeats local source identities in manifest constraints/overrides
        # and package metadata, often relative to the original lock directory.
        # Normalize every named requirement to the exact chosen bundle wheel.
        def rebase_requirements(value):
            if isinstance(value, dict):
                name = canonicalize_name(value.get("name", ""))
                if name in destinations and any(key in value for key in ("path", "url", "index")):
                    for key in ("url", "index"):
                        value.pop(key, None)
                    value["path"] = destinations[name].as_posix()
                for item in value.values():
                    rebase_requirements(item)
            elif isinstance(value, list):
                for item in value:
                    rebase_requirements(item)
        rebase_requirements(derivative_lock.get("manifest", {}))
        for item in derivative_lock["package"]:
            rebase_requirements(item.get("metadata", {}))
        derivative_input = rebase(deepcopy(data))
        derivative_input["environment"]["lockfile"] = str(final / "environment" / "uv.lock")
        for component in ([derivative_input["factor"]] if data["kind"] == "factor" else derivative_input["algos"].values()):
            if component:
                name = canonicalize_name(component["package"])
                if name not in destinations:
                    raise ArtifactError("Published component is absent from its local dependency closure")
                component["wheel"] = str(destinations[name])
        # Refuse references to another workspace/run hidden in config or input.
        shared = Path(SoloSettings.SHARED_DIR).absolute()
        forbidden = [str(shared / "projects"), str(shared / "runs"),
                     (shared / "projects").as_posix(), (shared / "runs").as_posix()]
        for document in (config, derivative_lock, derivative_input):
            serialized = json.dumps(document, ensure_ascii=False)
            if any(value in serialized for value in forbidden):
                raise ArtifactError("Published bundle still references a source workspace or run")
        environment = stage / "environment"
        environment.mkdir()
        (environment / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
        (environment / "uv.lock").write_text(tomlkit.dumps(derivative_lock), encoding="utf-8")
        (stage / "input.json").write_text(json.dumps(derivative_input, ensure_ascii=False, indent=2), encoding="utf-8")
        files, _ = _bundle_inventory(stage)
        root_snapshot = artifact_snapshot(artifact)
        root_snapshot["dependencies"] = {name: deepcopy(item) for name, item in snapshots.items()
                                         if name != artifact.package_name}
        (stage / "manifest.json").write_text(json.dumps({"artifact_id": str(artifact.id), "files": files,
            "root_artifact": root_snapshot,
            "original_input_sha256": hashlib.sha256((directory / "input.json").read_bytes()).hexdigest(),
            "original_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
            "accepted_artifacts": snapshots}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        expected = _bundle_inventory(stage)
        for relative in expected[0]:
            with checked_path(stage / relative, stage).open("rb+") as file:
                os.fsync(file.fileno())
        for relative in sorted(expected[1], key=lambda value: -len(Path(value).parts)):
            _sync_bundle_directory(stage / relative)
        _sync_bundle_directory(stage)
        if final.exists() or final.is_symlink() or final.is_junction():
            # A previous caller rolled back, crashed, or lost its commit ACK.
            # The locked DB row is still unpublished here. Compare every file
            # and directory to fresh truth; manifests may differ only in JSON
            # serialization order from older unsealed bundles. Preserve and
            # eventually DB-seal their original RAW bytes, never rewrite them.
            recovered_files, recovered_directories = _bundle_inventory(final)
            rebuilt_files, rebuilt_directories = expected
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate publication manifest key")
                    result[key] = value
                return result
            try:
                recovered_manifest = json.loads(checked_path(final / "manifest.json", final).read_bytes(),
                                                object_pairs_hook=unique_object)
                rebuilt_manifest = json.loads(checked_path(stage / "manifest.json", stage).read_bytes())
            except (ValueError, UnicodeError) as error:
                raise ArtifactError("Existing publication bundle is not the exact accepted recovery tree", "artifact_conflict") from error
            if (recovered_directories != rebuilt_directories or recovered_manifest != rebuilt_manifest
                    or set(recovered_files) != set(rebuilt_files)
                    or {key: value for key, value in recovered_files.items() if key != "manifest.json"}
                    != {key: value for key, value in rebuilt_files.items() if key != "manifest.json"}):
                raise ArtifactError("Existing publication bundle is not the exact accepted recovery tree", "artifact_conflict")
            _discard_bundle_stage(stage, artifact)
            return final
        stage.rename(final)
        _sync_bundle_directory(final.parent)
    except Exception:
        # Only the invocation-owned stage may be cleaned. A final directory must
        # survive every exception because the database commit may have succeeded.
        _discard_bundle_stage(stage, artifact)
        raise
    return final


def verify_bundle(artifact: ResearchArtifact) -> tuple[Path, dict]:
    if not artifact.published_at or not artifact.bundle_path:
        raise ArtifactError("Published artifact has no independent bundle")
    if not artifact.bundle_manifest_sha256 or not re.fullmatch(r"[0-9a-f]{64}", artifact.bundle_manifest_sha256):
        raise ArtifactError("Published bundle has no original database manifest seal", "artifact_tampered")
    expected = artifact_path(artifact).parent / "bundle"
    root = checked_path(artifact.bundle_path, artifact_path(artifact).parent, directory=True)
    if root != expected.resolve():
        raise ArtifactError("Artifact bundle path does not match its digest registry location")
    content = checked_path(root / "manifest.json", root).read_bytes()
    # Verify RAW bytes against immutable DB truth before trusting any paths or
    # derived input. Rewriting both input and its on-disk checksum cannot pass.
    if hashlib.sha256(content).hexdigest() != artifact.bundle_manifest_sha256:
        raise ArtifactError("Artifact bundle manifest changed after publication", "artifact_tampered")
    manifest = json.loads(content)
    files = manifest.get("files")
    if (manifest.get("artifact_id") != str(artifact.id) or not isinstance(files, dict)
            or manifest.get("root_artifact") != artifact_snapshot(artifact)):
        raise ArtifactError("Artifact bundle manifest does not match its immutable registry identity", "artifact_tampered")
    accepted = manifest.get("accepted_artifacts")
    if not isinstance(accepted, dict) or set(accepted) != {artifact.package_name, *(artifact.dependencies or {})}:
        raise ArtifactError("Artifact bundle has an incomplete accepted artifact manifest", "artifact_tampered")
    required = {"input.json", "environment/uv.lock", "environment/pyproject.toml",
                "original/input.json", "original/uv.lock", "original/pyproject.toml"}
    for name, snapshot in accepted.items():
        if not isinstance(snapshot, dict) or snapshot.get("package") != name:
            raise ArtifactError("Artifact bundle contains invalid accepted wheel metadata", "artifact_tampered")
        relative = f"wheels/{name}/{snapshot.get('filename')}"
        required.add(relative)
        if files.get(relative) != snapshot.get("sha256"):
            raise ArtifactError("Artifact bundle wheel is not bound to its exact accepted SHA", "artifact_tampered")
    root_snapshot = accepted[artifact.package_name]
    expected_root = artifact_snapshot(artifact)
    # The original accepted root predates publication's derived closure seal.
    if any(root_snapshot.get(key) != value for key, value in expected_root.items() if key != "dependencies"):
        raise ArtifactError("Artifact bundle root differs from its immutable artifact", "artifact_tampered")
    if any(accepted.get(name) != item for name, item in (artifact.dependencies or {}).items()):
        raise ArtifactError("Artifact bundle dependencies differ from its immutable closure", "artifact_tampered")
    if not required.issubset(files):
        raise ArtifactError("Artifact bundle manifest omits required frozen files", "artifact_tampered")
    for relative, digest in files.items():
        if (not isinstance(relative, str) or Path(relative).as_posix() != relative
                or Path(relative).is_absolute() or ".." in Path(relative).parts
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ArtifactError("Artifact bundle manifest contains invalid file identities", "artifact_tampered")
        file = checked_path((root / relative).absolute(), root)
        if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ArtifactError("Artifact bundle bytes changed after publication", "artifact_tampered")
    if (manifest.get("original_input_sha256") != files["original/input.json"]
            or manifest.get("original_lock_sha256") != files["original/uv.lock"]):
        raise ArtifactError("Artifact bundle lost its original accepted hashes", "artifact_tampered")
    data = json.loads(checked_path(root / "input.json", root).read_text(encoding="utf-8"))
    if data.get("kind") != artifact.kind:
        raise ArtifactError("Artifact bundle input kind differs from its immutable root", "artifact_tampered")
    component = policy.task_components(data).get(artifact.package_name)
    if component is None or any(component.get(key) != expected_root[key] for key in ("package", "version", "entry", "sha256")):
        raise ArtifactError("Artifact bundle input differs from its immutable root", "artifact_tampered")
    return root, data


def publish_version(session: Session, project: Project, version: ProjectVersion) -> ResearchArtifact:
    lock_registry(session)
    if version.project_id != project.id or version.status != "success":
        raise ArtifactError("Only the owning Project's successful saved result can be published")
    snapshots = version.accepted_artifacts
    if not version.input_sha256 or not version.lock_sha256 or not isinstance(snapshots, dict) or not snapshots:
        raise ArtifactError("Publication requires the original accepted hashes and artifact snapshots")
    package = project.package_name
    if package not in snapshots:
        raise ArtifactError("Saved result has no accepted own-root artifact snapshot")
    exact_artifacts(session, [snapshots[package]])
    artifact = require_artifact(session, snapshots[package])
    require_active(artifact)
    if artifact.kind != project.kind or artifact.package_name != package or artifact.version != version.package_version:
        raise ArtifactError("Accepted own-root artifact does not match the saved result")
    if artifact.origin_project_id != project.id:
        raise ArtifactError("Accepted own-root artifact belongs to a different original Project")
    if artifact.published_at is not None:
        verify_bundle(artifact)
        return artifact
    if artifact.bundle_path is not None or artifact.bundle_manifest_sha256 is not None:
        raise ArtifactError("Unpublished artifact has an incomplete database seal", "artifact_tampered")
    conflict = session.scalar(select(ResearchArtifact).where(ResearchArtifact.package_name == package,
        ResearchArtifact.version == artifact.version, ResearchArtifact.published_at.is_not(None)))
    if conflict is not None and conflict.id != artifact.id:
        raise ArtifactError("Different bytes are already published for this package/version", "artifact_conflict")
    directory = policy.run_path(version.id)
    data, lockfile, lock = policy.task_files(directory, project.kind,
        input_sha256=version.input_sha256, lock_sha256=version.lock_sha256)
    policy.check_task(session, version, project=project, directory=directory, kind=project.kind)
    actual, _ = policy.locked_scheme(lock)
    if artifact.scheme_version != str(actual) or version.scheme_version != str(actual):
        raise ArtifactError("Publication ActualScheme differs from the accepted saved result")
    component = data["factor"] if project.kind == "factor" else data["algos"][project.kind]
    own_wheel = policy.file_in_run(directory, component["wheel"])
    require_artifact(session, snapshots[package], wheel=own_wheel)
    if any(component.get(key) != snapshots[package].get(key) for key in ("package", "version", "entry", "sha256")):
        raise ArtifactError("Saved result component conflicts with its accepted root snapshot")
    report = json.loads(checked_path(directory / "report" / "run.json", directory).read_text(encoding="utf-8"))
    if (report.get("status") != "success" or report.get("input_sha256") != version.input_sha256
            or report.get("lock_sha256") != version.lock_sha256
            or report.get("versions", {}).get("scheme") != str(actual)):
        raise ArtifactError("Publication requires a successful report bound to the exact accepted input and lock")
    # The saved lock already captures the exact published environment. A root's
    # previous source dev/marker graph must not impose unrelated dependencies.
    exact_artifacts(session, list(snapshots.values()))
    # Preserve accepted snapshots, including descriptive upstream dependencies.
    # An upstream publication may seal its own graph later; it must not change
    # this saved run's closure or the deterministic rollback recovery candidate.
    closure = deepcopy(snapshots)
    for name, wheel in _local_assets(directory, lockfile, lock).items():
        if name in closure:
            require_artifact(session, closure[name], wheel=wheel)
        elif policy.RESEARCH_PACKAGE.fullmatch(name):
            raise ArtifactError(f"Research closure {name} is missing its accepted provenance")
        else:
            asset = register_asset(session, wheel, sources=artifact.sources, scheme_version=str(actual))
            closure[name] = artifact_snapshot(asset)
    closure.pop(package, None)
    bundle = _bundle(session, artifact, directory, data, lockfile, lock, {package: snapshots[package], **closure})
    artifact.dependencies = closure
    artifact.bundle_manifest_sha256 = hashlib.sha256(checked_path(bundle / "manifest.json", bundle).read_bytes()).hexdigest()
    artifact.bundle_path, artifact.published_at = str(bundle), utc_now()
    # Keep a complete final bundle across flush failures and caller rollbacks.
    # Retry can validate/adopt it; deleting here could erase a committed seal.
    session.flush()
    return artifact


def publish_version_committed(session: Session, project: Project, version: ProjectVersion) -> ResearchArtifact:
    """Own the publication commit and resolve a lost ACK from persisted DB truth.

    Final bundles are never removed on a transaction error: an unsealed exact
    bundle is recoverable by publish_version, while a committed seal is immutable.
    """
    try:
        artifact = publish_version(session, project, version)
    except Exception:
        session.rollback()
        raise
    identifier, digest = artifact.id, artifact.sha256
    seal, bundle = artifact.bundle_manifest_sha256, artifact.bundle_path
    dependencies = deepcopy(artifact.dependencies)
    try:
        session.commit()
    except Exception as error:
        try:
            session.rollback()
            lock_registry(session)
            persisted = session.scalar(select(ResearchArtifact).where(ResearchArtifact.id == identifier)
                .with_for_update().execution_options(populate_existing=True))
            if persisted is None or persisted.sha256 != digest:
                raise ArtifactError("Publication registry identity changed", "artifact_tampered")
            if persisted.published_at is not None:
                if (persisted.bundle_manifest_sha256 != seal or persisted.bundle_path != bundle
                        or persisted.dependencies != dependencies):
                    raise ArtifactError("Publication committed a conflicting bundle seal", "artifact_tampered")
                require_artifact(session, persisted.id)
                verify_bundle(persisted)
                session.rollback()  # Release the recovery read's registry lock.
                return persisted
            if persisted.bundle_manifest_sha256 is not None or persisted.bundle_path is not None:
                raise ArtifactError("Publication has an incomplete database seal", "artifact_tampered")
        except Exception as truth_error:
            session.rollback()
            raise ArtifactError("Cannot confirm publication commit state; bundle retained, retry the same saved version",
                                "artifact_publish_state_unknown") from truth_error
        session.rollback()
        raise error
    return artifact


def artifact_component(session: Session, identifier: UUID | str) -> dict:
    # The root's DB-sealed closure is already the exact accepted lock selection,
    # not a graph of the dependencies' historical development environments.
    exact_artifacts(session, [identifier])
    artifact = require_artifact(session, identifier)
    require_active(artifact)
    if artifact.published_at is None or artifact.kind == "dependency":
        raise ArtifactError("Strategy selection requires a published research artifact")
    root, data = verify_bundle(artifact)
    closure = {artifact.package_name: artifact_snapshot(artifact), **deepcopy(artifact.dependencies)}
    current = exact_artifacts(session, list(closure.values()))
    lockfile = checked_path(root / "environment/uv.lock", root)
    lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    scheme = policy.require_locked_scheme(lock)
    if str(scheme) != artifact.scheme_version:
        raise ArtifactError("Published lock Scheme differs from its immutable root", "artifact_tampered")
    assets = _local_assets(root, lockfile, lock)
    if set(closure) != set(current) or set(closure) != set(policy.local_task_packages(lock)) or set(closure) != set(assets):
        raise ArtifactError("Published closure is not the exact sealed local lock selection", "artifact_tampered")
    for name, item in closure.items():
        # Every immutable identity field must still match. A dependency may have
        # sealed its own descriptive graph after this root accepted its bytes;
        # that graph is neither selected nor recursively validated here.
        if ({key: value for key, value in item.items() if key != "dependencies"}
                != {key: value for key, value in current[name].items() if key != "dependencies"}):
            raise ArtifactError("Published dependency differs from its exact sealed snapshot", "artifact_tampered")
        require_artifact(session, item, wheel=assets[name])
        if item["kind"] != "dependency":
            actual = policy.formal_version(item["scheme_version"])
            if (actual.major, actual.minor) != (scheme.major, scheme.minor):
                raise ArtifactError("Published closure uses a different Scheme major/minor")
    component = deepcopy(data["factor"] if artifact.kind == "factor" else data["algos"][artifact.kind])
    component.update(wheel=str(artifact_path(artifact)), artifact_id=str(artifact.id))
    snapshot = artifact_snapshot(artifact)
    component_snapshot = {key: snapshot[key] for key in ("package", "version", "entry", "sha256")}
    return {"label": f"{artifact.origin_project_name or artifact.package_name} · {artifact.version}",
        "kind": artifact.kind, "artifact_id": str(artifact.id), "version_id": None,
        "project_id": str(artifact.origin_project_id) if artifact.origin_project_id else None,
        "scheme_version": artifact.scheme_version, "root_sources": deepcopy(artifact.sources),
        "component": component, "component_snapshot": component_snapshot, "input": data,
        "environment_path": str(root / "environment"), "published": True,
        "dependencies": deepcopy(artifact.dependencies),
        "accepted_artifacts": {artifact.package_name: snapshot, **deepcopy(artifact.dependencies)}}


def gc_unreferenced(session: Session) -> dict[str, int]:
    """Remove only unreferenced unpublished objects; no origin joins decide liveness."""
    from core.apps.strategies.models import Strategy
    lock_registry(session)
    # Register/protect/accept lock the same artifact rows. Read references AFTER
    # these locks so a concurrent completed registration cannot be swept using a
    # stale liveness snapshot. Short-lived rows are not a permanent retention pool.
    artifacts = list(session.scalars(select(ResearchArtifact).order_by(ResearchArtifact.id).with_for_update()))
    by_id = {str(item.id): item for item in artifacts}
    retained = {str(item.id) for item in artifacts if item.published_at is not None}
    refs = []
    now = utc_now()
    consumers = {project.id: project for project in session.scalars(select(Project))}
    for project in consumers.values():
        refs.extend(_snapshot_values(project.artifact_dependencies or {}))
    for installation in session.scalars(select(ArtifactInstallation)):
        if (installation.consumer_project_id not in consumers or consumers[installation.consumer_project_id].archived
                or not installation_references_live(installation, now)):
            session.delete(installation)
        else:
            refs.extend(_snapshot_values(installation.artifacts or {}))
    for record in list(session.scalars(select(ProjectVersion))) + list(session.scalars(select(Strategy))):
        refs.extend(_snapshot_values(record.accepted_artifacts or {}))
        if hasattr(record, "components"):
            for component in (record.components or {}).values():
                if component.get("artifact_id"):
                    retained.add(str(component["artifact_id"]))
    retained.update(str(item.get("artifact_id") or item.get("id")) for item in refs)
    pending = list(retained)
    visited = set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        artifact = by_id.get(identifier)
        if artifact:
            for snapshot in _snapshot_values(artifact.dependencies or {}):
                dependency = str(snapshot.get("artifact_id") or snapshot.get("id"))
                retained.add(dependency)
                pending.append(dependency)
    removed = 0
    for artifact in artifacts:
        if str(artifact.id) in retained:
            continue
        directory = artifact_path(artifact).parent
        if directory.exists():
            checked_path(directory, Path(SoloSettings.SHARED_DIR).absolute() / "artifacts", directory=True)
            # Walk before deleting: a replaced/link child must not be followed.
            for child in directory.rglob("*"):
                if child.is_symlink() or child.is_junction():
                    raise ArtifactError("Unpublished artifact cleanup contains a link")
            shutil.rmtree(directory)
        session.delete(artifact)
        removed += 1
    session.flush()
    return {"removed": removed, "retained": len(artifacts) - removed}


def backfill_tasks(session: Session) -> dict:
    from .backfill import backfill_tasks as migrate_tasks
    return migrate_tasks(session)
