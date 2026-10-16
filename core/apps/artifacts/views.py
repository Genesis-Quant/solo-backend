"""Local registration and source-independent artifact endpoints."""

from typing import Annotated, TypeAlias
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion
from core.database.session import get_database_session
from .deletion import delete_artifact
from .models import ResearchArtifact
from .service import (ArtifactError, artifact_directories, artifact_path, checked_path, exact_artifacts,
                      lock_registry, publish_version_committed, read_artifact, register_file, require_artifact)

router = APIRouter(prefix="/api/v1", tags=["artifacts"])
Database: TypeAlias = Annotated[Session, Depends(get_database_session)]


class RegisterArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    wheel: str = Field(min_length=1)
    entry: str | None = None


class AcceptDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[UUID]


class PublishArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _error(error) -> HTTPException:
    if isinstance(error, IntegrityError):
        return HTTPException(409, {"code": "artifact_conflict", "reason": "A published package/version already exists"})
    if isinstance(error, SQLAlchemyError):
        return HTTPException(503, {"code": "artifact_transaction_failed", "reason": "Artifact transaction failed"})
    detail = error.detail if isinstance(error, (ArtifactError, policy.VersionPolicyError)) else {
        "code": "invalid_artifact", "reason": str(error),
    }
    return HTTPException(404 if getattr(error, "code", "") == "artifact_not_found" else
                         409 if getattr(error, "code", "") == "artifact_conflict" else 422, detail)


def _project(session: Session, identifier: UUID, *, lock: bool = False) -> Project:
    if lock:
        lock_registry(session)
    project = (session.scalar(select(Project).where(Project.id == identifier).with_for_update())
               if lock else session.get(Project, identifier))
    if project is None or project.archived:
        raise HTTPException(404, "Project does not exist")
    return project


@router.get("/artifacts")
def list_artifacts(session: Database, published: bool | None = None,
                   kind: str | None = Query(default=None, pattern=r"^(factor|model|optimize|control|execution|dependency)$")) -> list[dict]:
    query = select(ResearchArtifact)
    if published is not None:
        query = query.where(ResearchArtifact.published_at.is_not(None) if published else ResearchArtifact.published_at.is_(None))
    if kind is not None:
        query = query.where(ResearchArtifact.kind == kind)
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
        lock_registry(session)
        artifact = require_artifact(session, artifact_id)
        wheel = checked_path(artifact_path(artifact))
    except (ArtifactError, OSError, ValueError) as error:
        raise _error(error) from error
    return FileResponse(wheel, filename=artifact.filename, media_type="application/octet-stream")


@router.post("/artifacts/register", status_code=201)
def register_artifact(body: RegisterArtifact, session: Database) -> dict:
    try:
        source = _project(session, body.project_id, lock=True)
        wheel = checked_path(body.wheel)
        actual = policy.require_project(source)
        with artifact_directories() as directories:
            artifact = register_file(session, wheel, source, component={"entry": body.entry},
                                     scheme_version=str(actual), created=directories)
            session.commit()
        return read_artifact(artifact)
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
        session.rollback()
        raise _error(error) from error


@router.post("/projects/{consumer_id}/dependencies")
def accept_dependencies(consumer_id: UUID, body: AcceptDependencies, session: Database) -> dict:
    try:
        consumer = _project(session, consumer_id, lock=True)
        actual = policy.require_project(consumer)
        snapshots = exact_artifacts(session, body.artifacts)
        for snapshot in snapshots.values():
            if snapshot["kind"] != "dependency":
                scheme = policy.formal_version(snapshot["scheme_version"])
                if (scheme.major, scheme.minor) != (actual.major, actual.minor):
                    raise ArtifactError("Research dependencies must use the consumer's Scheme series")
        consumer.artifact_dependencies = snapshots
        session.commit()
        return {"artifacts": list(snapshots.values())}
    except (ArtifactError, policy.VersionPolicyError, OSError, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
        session.rollback()
        raise _error(error) from error


@router.post("/projects/{project_id}/versions/{version_id}/publish")
def publish(project_id: UUID, version_id: UUID, body: PublishArtifact, session: Database) -> dict:
    from core.apps.projects.versions import read_version
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
