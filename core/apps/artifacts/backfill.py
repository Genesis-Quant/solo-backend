"""Explicit one-shot legacy snapshot migration; no live migration on reads/startup."""

from copy import deepcopy
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies.models import Strategy


def backfill_tasks(session: Session) -> dict:
    """Import only provable frozen bytes. The caller backs up and commits explicitly.

    Strategies are processed first, before any source purge. Missing sources stay
    fail-closed with a record-specific result. NULL input/lock hashes remain NULL.
    Retired history may be captured, but policy still denies its new use.
    """
    stats = {"migrated": 0, "skipped": 0, "failed": 0, "records": []}
    records = list(session.scalars(select(Strategy).order_by(Strategy.created_at, Strategy.id)))
    records += list(session.scalars(select(ProjectVersion).order_by(ProjectVersion.created_at, ProjectVersion.id)))
    for record in records:
        kind = "strategy" if isinstance(record, Strategy) else None
        identifier = str(record.id)
        if record.accepted_artifacts:
            stats["skipped"] += 1
            stats["records"].append({"id": identifier, "type": kind or "project", "status": "already_snapshotted"})
            continue
        original_hashes = (record.input_sha256, record.lock_sha256)
        try:
            with session.begin_nested():
                project = session.get(Project, record.project_id) if isinstance(record, ProjectVersion) else None
                if isinstance(record, ProjectVersion) and project is None:
                    raise ValueError("Legacy task no longer has an authoritative source Project")
                kind = kind or project.kind
                snapshots = policy.capture_task_artifacts(session, record, project=project,
                    directory=policy.run_path(record.id), kind=kind, historical=True)
                if not snapshots:
                    raise ValueError("Legacy frozen task has no provable local artifact snapshots")
                record.accepted_artifacts = snapshots
                if isinstance(record, Strategy):
                    components = deepcopy(record.components)
                    for stage, component in components.items():
                        if not component.get("version_id") and not component.get("artifact_id"):
                            continue
                        matches = [item for item in snapshots.values() if item["kind"] == stage and (
                            not component.get("project_id") or item.get("origin_project_id") == component["project_id"])]
                        if len(matches) != 1:
                            raise ValueError("Legacy strategy selected component is missing or ambiguous")
                        snapshot, = matches
                        component.update(artifact_id=snapshot["artifact_id"],
                            component_snapshot={key: snapshot[key] for key in ("package", "version", "entry", "sha256")},
                            root_sources=deepcopy(snapshot["sources"]), scheme_version=snapshot["scheme_version"])
                    record.components = components
                if (record.input_sha256, record.lock_sha256) != original_hashes:
                    raise ValueError("Snapshot backfill must never invent or change accepted hashes")
                session.flush()
        except (ValueError, OSError, KeyError, TypeError) as error:
            stats["failed"] += 1
            stats["records"].append({"id": identifier, "type": kind or "project", "status": "incomplete",
                                     "reason": str(error)})
        else:
            stats["migrated"] += 1
            stats["records"].append({"id": identifier, "type": kind, "status": "snapshotted",
                "artifacts": len(record.accepted_artifacts), "accepted_hashes_present": all(original_hashes)})
    return stats
