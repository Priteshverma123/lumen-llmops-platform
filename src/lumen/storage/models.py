"""Postgres (transactional state): projects, keys, prompt registry, experiments, datasets,
annotation queues, alerting and the embedding model registry. Telemetry lives in ClickHouse."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[str]: ARRAY(String), uuid.UUID: UUID(as_uuid=True)}


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _project() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _now()


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    name: Mapped[str] = mapped_column(String(200))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(default=list)  # ingest | read | write | admin
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------------------- prompt registry
class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now()


class PromptVersion(Base):
    """Immutable (enforced by a trigger in the migration): a version is a fact, never edited."""

    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("prompt_id", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    prompt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    messages: Mapped[dict[str, Any]] = mapped_column()  # {"messages": [{"role", "content"}]}, Jinja2 templates
    variables_schema: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model_config_: Mapped[dict[str, Any]] = mapped_column("model_config", default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    commit_message: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    warnings: Mapped[list[str]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _now()


class PromptLabel(Base):
    __tablename__ = "prompt_labels"
    prompt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"), primary_key=True)
    label: Mapped[str] = mapped_column(String(50), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class LabelHistory(Base):
    __tablename__ = "label_history"
    id: Mapped[uuid.UUID] = _pk()
    prompt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(50))
    from_version: Mapped[int | None] = mapped_column(Integer)
    to_version: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(200), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    override: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class EvalRun(Base):
    """Offline eval results linked to a prompt version: the promotion gate's evidence."""

    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prompt_versions.id", ondelete="SET NULL"))
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    git_sha: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------------------- experiments
class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    key: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(30), default="user")  # user | session | tenant
    # [{"name": "control", "weight": 0.5, "prompt": "rag-answer", "version": 3}, ...]
    variants: Mapped[dict[str, Any]] = mapped_column()
    primary_metric: Mapped[dict[str, Any]] = mapped_column()  # {"score": "faithfulness", "type": "mean"}
    guardrails: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {"error_rate": 0.02, "cost_ratio": 1.3}
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|running|stopped|completed
    stop_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------------------- datasets
class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now()


class DatasetItem(Base):
    __tablename__ = "dataset_items"
    id: Mapped[uuid.UUID] = _pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    input: Mapped[dict[str, Any]] = mapped_column()
    expected: Mapped[dict[str, Any]] = mapped_column(default=dict)
    item_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    source_trace_id: Mapped[str | None] = mapped_column(String(32))  # promoted from production traffic
    created_at: Mapped[datetime] = _now()


class DatasetRun(Base):
    __tablename__ = "dataset_runs"
    id: Mapped[uuid.UUID] = _pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)  # prompt version, model, git sha
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _now()


class DatasetRunItem(Base):
    __tablename__ = "dataset_run_items"
    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_runs.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_items.id", ondelete="CASCADE"))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    scores: Mapped[dict[str, Any]] = mapped_column(default=dict)


# ----------------------------------------------------------------------------- annotation queues
class AnnotationQueue(Base):
    __tablename__ = "annotation_queues"
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    name: Mapped[str] = mapped_column(String(200))
    score_names: Mapped[list[str]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _now()


class AnnotationItem(Base):
    __tablename__ = "annotation_items"
    __table_args__ = (Index("ix_annotation_pending", "queue_id", "status"),)
    id: Mapped[uuid.UUID] = _pk()
    queue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("annotation_queues.id", ondelete="CASCADE"))
    trace_id: Mapped[str] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")  # e.g. "low groundedness 0.31"
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|locked|done|skipped
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------------------- alerting
class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = _project()
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # slo_latency|slo_errors|cost_anomaly|quality_drop|cache_drop|drift
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id: Mapped[uuid.UUID] = _pk()
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(20))  # firing | resolved
    value: Mapped[float] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------------------- embeddings
class EmbeddingModel(Base):
    __tablename__ = "embedding_models"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # "bge-small@1"
    backend: Mapped[str] = mapped_column(String(30))  # tei | openai
    upstream_model: Mapped[str] = mapped_column(String(200))
    dim: Mapped[int] = mapped_column(Integer)
    max_tokens: Mapped[int] = mapped_column(Integer, default=512)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | deprecated
    price_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _now()


class ReembedJob(Base):
    __tablename__ = "reembed_jobs"
    id: Mapped[uuid.UUID] = _pk()
    from_model: Mapped[str] = mapped_column(String(200))
    to_model: Mapped[str] = mapped_column(String(200))
    source_uri: Mapped[str] = mapped_column(Text)  # s3://bucket/prefix of JSONL {"id","text"}
    target_uri: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {"done": n, "total": m, "cursor": "..."}
    created_at: Mapped[datetime] = _now()
