"""Internal local registration and public source-independent artifact endpoints."""

import hashlib
import json
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from packaging.utils import canonicalize_name
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion, utc_now
from core.database.session import get_database_session
from .deletion import delete_artifact
from .models import ArtifactInstallation, ResearchArtifact
from .service import (INSTALLATION_TTL, ArtifactError, artifact_path, checked_path, exact_artifacts,
                      lock_registry, protect_installation, publish_version_committed, read_artifact, read_installation,
                      register_file, require_artifact, require_installation, require_settled_installations)

router = APIRouter(prefix="/api/v1", tags=["artifacts"])
type Database = Annotated[Session, Depends(get_database_session)]


class RegisterArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    consumer_project_id: UUID | None = None
    install_id: UUID | None = None
    wheel: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry: str | None = Field(default=None, max_length=512)


class AcceptDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[UUID] = Field(max_length=2048)
    install_id: UUID | None = None


class InstallDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    install_id: UUID
    artifacts: list[UUID] = Field(default_factory=list, max_length=2048)
    renew: bool = False


class PublishArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _error(error) -> HTTPException:
    if isinstance(error, IntegrityError):
        return HTTPException(409, {"code": "artifact_conflict", "reason": "An artifact identity or published package/version already exists"})
    if isinstance(error, SQLAlchemyError):
        return HTTPException(503, {"code": "artifact_transaction_failed", "reason": "Artifact transaction could not be confirmed; retry the same request"})
    if getattr(error, "code", "") == "artifact_publish_state_unknown":
        return HTTPException(503, error.detail)
    detail = error.detail if isinstance(error, (ArtifactError, policy.VersionPolicyError)) else {
        "code": "invalid_artifact", "reason": "Artifact source, immutable metadata or frozen environment could not be verified",
    }
    return HTTPException(404 if getattr(error, "code", "") == "artifact_not_found" else
                         409 if getattr(error, "code", "") == "artifact_conflict" else 422, detail)


def _project(session: Session, identifier: UUID, *, lock: bool = False) -> Project:
    if lock:
        lock_registry(session)
    project = (session.scalar(select(Project).where(Project.id == identifier).with_for_update()
               .execution_options(populate_existing=True)) if lock else session.get(Project, identifier))
    if project is None or project.archived:
        raise HTTPException(404, "Project does not exist")
    return project


def _workspace(project: Project) -> Path:
    directory = checked_path(policy.project_path(project), directory=True)
    metadata = json.loads(checked_path(directory / ".solo", directory).read_text(encoding="utf-8"))
    if any(metadata.get(key) != value for key, value in {
        "project_id": str(project.id), "kind": project.kind, "name": project.name,
    }.items()):
        raise ArtifactError("Consumer workspace is not owned by its original Project identity")
    return directory


@router.get("/artifacts")
def list_artifacts(session: Database, published: bool | None = None,
                   kind: str | None = Query(default=None, pattern=r"^(factor|model|optimize|control|execution|dependency)$"),
                   sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$")) -> list[dict]:
    query = select(ResearchArtifact)
    if published is not None:
        query = query.where(ResearchArtifact.published_at.is_not(None) if published else ResearchArtifact.published_at.is_(None))
    if kind is not None:
        query = query.where(ResearchArtifact.kind == kind)
    if sha256 is not None:
        query = query.where(ResearchArtifact.sha256 == sha256)
    return [read_artifact(item) for item in session.scalars(query.order_by(ResearchArtifact.created_at.desc(), ResearchArtifact.id))]


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: UUID, session: Database) -> dict:
    artifact = session.get(ResearchArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "Artifact does not exist")
    return read_artifact(artifact)


@router.delete("/artifacts/{artifact_id}", status_code=204, response_class=Response)
def remove_artifact(artifact_id: UUID, session: Database) -> Response:
    delete_artifact(artifact_id, session)
    return Response(status_code=204)


@router.get("/artifacts/{artifact_id}/wheel")
def download_wheel(artifact_id: UUID, session: Database) -> FileResponse:
    try:
        # Keep the transaction lock until the response-scoped session closes,
        # so withdrawal cannot move a wheel while FileResponse is sending it.
        lock_registry(session)
        artifact = require_artifact(session, artifact_id)
    except (ArtifactError, OSError, ValueError) as error:
        raise _error(error) from error
    return FileResponse(artifact_path(artifact), filename=artifact.filename, media_type="application/octet-stream",
                        headers={"ETag": f'"sha256:{artifact.sha256}"'})


@router.post("/artifacts/register", status_code=201)
def register_artifact(body: RegisterArtifact, session: Database) -> dict:
    try:
        lock_registry(session)
        wheel = checked_path(body.wheel)
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != body.sha256:
            raise ArtifactError("Registration wheel differs from the supplied exact sha256", "artifact_tampered")
        source = session.get(Project, body.project_id)
        existing = session.scalar(select(ResearchArtifact).where(ResearchArtifact.sha256 == body.sha256))
        if source is None and (existing is None or existing.origin_project_id != body.project_id):
            raise ArtifactError("Registration requires the authoritative original Project owner")
        if source is not None and source.archived and existing is None:
            raise ArtifactError("An archived source Project cannot register new artifacts")
        consumer = None
        if body.consumer_project_id is not None:
            if body.install_id is None:
                raise ArtifactError("Consumer registration requires a durable pending installation nonce", "artifact_conflict")
            consumer = _project(session, body.consumer_project_id, lock=True)
            require_installation(session, consumer.id, body.install_id)
            directory = _workspace(consumer)
            checked_path(wheel, directory / ".solo-wheels")
            if existing is not None:
                actual = existing.scheme_version
            else:
                # Fresh producer registrations use the ORIGINAL live source's
                # lock, never the consumer's Scheme patch/commit or source state.
                source_directory = _workspace(source)
                source_lock = tomllib.loads(checked_path(source_directory / "uv.lock", source_directory).read_text(encoding="utf-8"))
                actual = policy.require_project(source, lock=source_lock)
        elif body.install_id is not None:
            raise ArtifactError("A pending installation must have an explicit consumer Project")
        elif source is not None:
            directory = policy.project_path(source)
            runs = Path(SoloSettings.SHARED_DIR).absolute() / "runs"
            if wheel.is_relative_to(directory.absolute()):
                checked_path(wheel, _workspace(source))
                actual = policy.require_project(source)
            elif wheel.is_relative_to(runs):
                try:
                    version_id = UUID(wheel.relative_to(runs).parts[0])
                except (ValueError, IndexError) as error:
                    raise ArtifactError("Registration run path is not an owned version UUID") from error
                version = session.get(ProjectVersion, version_id)
                if version is None or version.project_id != source.id:
                    raise ArtifactError("Registration run is not owned by its declared source Project")
                data, _, lock = policy.task_files(runs / str(version_id), source.kind)
                actual, _ = policy.locked_scheme(lock)
            else:
                raise ArtifactError("Registration wheel must belong to the source run/workspace or consumer .solo-wheels")
        else:
            raise ArtifactError("Registration after source deletion requires a consumer-owned frozen wheel")
        component = {"sha256": body.sha256}
        if body.entry is not None:
            component["entry"] = body.entry
        artifact = register_file(session, wheel, source, component=component,
            scheme_version=str(actual) if actual is not None else None)
        if consumer is not None:
            # Row and registry bytes become GC-protected in the SAME commit.
            protect_installation(session, consumer, body.install_id, [artifact.id])
        session.commit()
        return read_artifact(artifact)
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, IntegrityError) as error:
        session.rollback()
        raise _error(error) from error


def _workspace_wheel(directory: Path, lockfile: Path, item: dict) -> Path:
    """Installed workspace research sources must point to owned .solo-wheels."""
    source = item.get("source", {})
    if set(source) == {"path"}:
        wheel = policy.local_lock_path(lockfile.parent, source["path"])
    elif set(source) == {"registry"} and (Path(source["registry"]).is_absolute() or urlsplit(source["registry"]).scheme in {"", "file"}):
        registry = policy.local_lock_path(lockfile.parent, source["registry"])
        locked = item.get("wheels", [])
        if len(locked) != 1 or ("path" in locked[0]) == ("url" in locked[0]):
            raise ArtifactError("Installed registry must select one exact local wheel")
        entry = locked[0]
        wheel = policy.local_lock_path(registry if "path" in entry else lockfile.parent, entry.get("path", entry.get("url")))
        if not wheel.is_relative_to(registry):
            raise ArtifactError("Installed locked wheel escapes its local registry")
        if entry.get("hash") and entry["hash"] != "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest():
            raise ArtifactError("Installed wheel hash differs from its exact lock")
    else:
        raise ArtifactError("Installed artifact must have a local frozen wheel source")
    # Every provided lock hash must agree even for direct path sources.
    for locked in item.get("wheels", []):
        if locked.get("hash") and locked["hash"] != "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest():
            raise ArtifactError("Installed wheel hash differs from its exact lock", "artifact_tampered")
    return checked_path(wheel, directory / ".solo-wheels")


def _installed_dependencies(consumer: Project, session: Session, identifiers, *, active: bool = True) -> dict:
    directory = _workspace(consumer)
    lockfile = checked_path(directory / "uv.lock", directory)
    lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    actual = policy.require_project(consumer, lock=lock) if active else policy.locked_scheme(lock)[0]
    packages = {}
    for item in lock["package"]:
        name = canonicalize_name(item["name"])
        if name in packages:
            raise ArtifactError("Installed lock must select one exact version of each package")
        packages[name] = item
    required = set()
    for name, item in packages.items():
        source = item.get("source", {})
        local = "path" in source or ("registry" in source and (
            Path(source["registry"]).is_absolute() or urlsplit(source["registry"]).scheme in {"", "file"}))
        if name not in {consumer.package_name, "scheme"} and (local or policy.RESEARCH_PACKAGE.fullmatch(name)):
            required.add(name)
        if {"editable", "directory"} & source.keys() and name != consumer.package_name:
            raise ArtifactError("Installed upstream sources must be exact frozen wheels")
    snapshots = exact_artifacts(session, identifiers, active=active)
    if set(snapshots) != required:
        raise ArtifactError("Dependency acceptance must cover the exact installed research and local wheel assets")
    for name, snapshot in snapshots.items():
        artifact = require_artifact(session, snapshot)
        package = packages[name]
        if package["version"] != artifact.version:
            raise ArtifactError("Accepted artifact is not the exact installed lock version")
        require_artifact(session, snapshot, wheel=_workspace_wheel(directory, lockfile, package))
        if artifact.kind != "dependency":
            scheme = policy.formal_version(artifact.scheme_version)
            if (scheme.major, scheme.minor) != (actual.major, actual.minor):
                raise ArtifactError("Installed research artifacts must use the consumer's frozen Scheme series")
            from core.apps.projects.templates import validate_template
            from .service import wheel_metadata
            _, _, requirements, _ = wheel_metadata(artifact_path(artifact))
            validate_template({"version": artifact.version, "dependencies": requirements}, actual)
    return snapshots


@router.post("/projects/{consumer_id}/dependencies/installations")
def begin_installation(consumer_id: UUID, body: InstallDependencies, session: Database) -> dict:
    try:
        consumer = _project(session, consumer_id, lock=True)
        _workspace(consumer)
        policy.require_project(consumer)
        if body.renew:
            require_installation(session, consumer.id, body.install_id)
        installation = protect_installation(session, consumer, body.install_id, body.artifacts)
        session.commit()
        return read_installation(installation)
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, IntegrityError) as error:
        session.rollback()
        raise _error(error) from error


@router.post("/projects/{consumer_id}/dependencies")
def accept_dependencies(consumer_id: UUID, body: AcceptDependencies, session: Database) -> dict:
    try:
        consumer = _project(session, consumer_id, lock=True)
        installation = require_installation(session, consumer.id, body.install_id) if body.install_id else None
        require_settled_installations(session, consumer.id, body.install_id)
        snapshots = _installed_dependencies(consumer, session, body.artifacts)
        if installation is not None:
            receipt = deepcopy(installation.artifacts)
            if receipt["accepted"] is not None:
                if receipt["accepted"] != snapshots or consumer.artifact_dependencies != snapshots:
                    raise ArtifactError("Installation receipt conflicts with the currently accepted environment", "artifact_conflict")
            elif consumer.artifact_dependencies != receipt["before"]:
                raise ArtifactError("Another installation changed the accepted environment", "artifact_conflict")
            receipt["accepted"] = snapshots
            installation.artifacts = receipt
            installation.expires_at = utc_now() + INSTALLATION_TTL
        consumer.artifact_dependencies = snapshots
        session.commit()
        return {"artifacts": list(snapshots.values())}
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, IntegrityError) as error:
        session.rollback()
        raise _error(error) from error


@router.post("/projects/{consumer_id}/dependencies/installations/{install_id}/complete")
def complete_installation(consumer_id: UUID, install_id: UUID, session: Database) -> dict:
    try:
        consumer = _project(session, consumer_id, lock=True)
        installation = session.get(ArtifactInstallation, install_id)
        if installation is not None:
            installation = require_installation(session, consumer.id, install_id, allow_expired=True)
            receipt = installation.artifacts
            if receipt["accepted"] is None or consumer.artifact_dependencies != receipt["accepted"]:
                raise ArtifactError("Installation has not accepted its final environment", "artifact_conflict")
            _installed_dependencies(consumer, session, receipt["accepted"].values(), active=False)
            session.delete(installation)
        session.commit()
        return {"install_id": str(install_id), "completed": True}
    except (ArtifactError, OSError, ValueError, KeyError, TypeError, IntegrityError) as error:
        session.rollback()
        raise _error(error) from error


@router.post("/projects/{consumer_id}/dependencies/installations/{install_id}/abort")
def abort_installation(consumer_id: UUID, install_id: UUID, session: Database) -> dict:
    try:
        consumer = _project(session, consumer_id, lock=True)
        installation = session.get(ArtifactInstallation, install_id)
        if installation is not None:
            installation = require_installation(session, consumer.id, install_id, allow_expired=True)
            receipt = installation.artifacts
            if receipt["accepted"] is not None:
                # Abort follows filesystem rollback, including a lost acceptance
                # reply. Never overwrite another installation's accepted state.
                if consumer.artifact_dependencies not in (receipt["accepted"], receipt["before"]):
                    raise ArtifactError("Another installation changed the accepted environment", "artifact_conflict")
                _installed_dependencies(consumer, session, receipt["before"].values(), active=False)
                consumer.artifact_dependencies = deepcopy(receipt["before"])
            session.delete(installation)
        session.commit()
        return {"install_id": str(install_id), "aborted": True}
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, IntegrityError) as error:
        session.rollback()
        raise _error(error) from error


@router.post("/projects/{project_id}/versions/{version_id}/publish")
def publish(project_id: UUID, version_id: UUID, body: PublishArtifact, session: Database) -> dict:
    from core.apps.projects.versions import read_version
    lock_registry(session)
    project = _project(session, project_id)
    version = session.get(ProjectVersion, version_id)
    if version is None or version.project_id != project_id:
        raise HTTPException(404, "Saved version does not exist")
    try:
        artifact = publish_version_committed(session, project, version)
        result = read_version(version)
        result.update(publishStatus="published", artifactId=str(artifact.id), artifact=read_artifact(artifact))
        return result
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
        session.rollback()
        raise _error(error) from error
