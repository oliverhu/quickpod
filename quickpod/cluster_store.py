"""Persist cluster metadata (SQLite under ~/.quickpod by default, or Postgres via URL)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from quickpod.runpod_client import (
    count_alive_nodes,
    count_ready_nodes,
    list_managed_pods,
)
from quickpod.spec import ClusterSpec


class Base(DeclarativeBase):
    pass


class ClusterRecord(Base):
    __tablename__ = "clusters"

    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    num_nodes: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    launches: Mapped[list["PodLaunchRecord"]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )


class PodLaunchRecord(Base):
    __tablename__ = "pod_launches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_name: Mapped[str] = mapped_column(
        String(256), ForeignKey("clusters.name", ondelete="CASCADE"), nullable=False
    )
    pod_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pod_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    launched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    cluster: Mapped["ClusterRecord"] = relationship(back_populates="launches")


class ServeDaemonRecord(Base):
    """Background `quickpod serve` worker (OpenAI proxy + per-cluster UI) per cluster."""

    __tablename__ = "serve_daemons"

    cluster_name: Mapped[str] = mapped_column(
        String(256), ForeignKey("clusters.name", ondelete="CASCADE"), primary_key=True
    )
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ServeLaunchPrefs(Base):
    """Last `quickpod serve` options per cluster (survives `clusters stop` for `quickpod refresh`)."""

    __tablename__ = "serve_launch_prefs"

    cluster_name: Mapped[str] = mapped_column(
        String(256), ForeignKey("clusters.name", ondelete="CASCADE"), primary_key=True
    )
    spec_path: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    reconcile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ssl_certfile: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssl_keyfile: Mapped[str | None] = mapped_column(Text, nullable=True)


_engines: dict[str, Any] = {}


def default_sqlite_url() -> str:
    root = Path.home() / ".quickpod"
    root.mkdir(parents=True, exist_ok=True)
    db = root / "state.db"
    return f"sqlite:///{db.resolve().as_posix()}"


def resolve_database_url(cli_override: str | None) -> str:
    if cli_override and cli_override.strip():
        return cli_override.strip()
    env = os.environ.get("QUICKPOD_DATABASE_URL")
    if env and env.strip():
        return env.strip()
    return default_sqlite_url()


def _make_engine(url: str):
    if url not in _engines:
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        try:
            _engines[url] = create_engine(url, future=True, **kwargs)
        except ModuleNotFoundError as e:
            raise RuntimeError(
                f"Database driver missing for {url.split(':', 1)[0]}: {e}. "
                "For PostgreSQL install psycopg (e.g. uv add psycopg[binary])."
            ) from e
    return _engines[url]


def init_db(database_url: str | None = None) -> str:
    url = resolve_database_url(database_url)
    eng = _make_engine(url)
    Base.metadata.create_all(eng)
    return url


def upsert_cluster_touch(
    spec: ClusterSpec,
    *,
    spec_path: str | None,
    database_url: str | None = None,
) -> None:
    """Record or update cluster metadata (call on every reconcile pass)."""
    url = init_db(database_url)
    now = datetime.now(timezone.utc)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ClusterRecord, spec.name)
        if row is None:
            session.add(
                ClusterRecord(
                    name=spec.name,
                    num_nodes=spec.num_nodes,
                    spec_path=spec_path,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.num_nodes = spec.num_nodes
            if spec_path is not None:
                row.spec_path = spec_path
            row.updated_at = now
        session.commit()


def record_pod_launch(
    cluster_name: str,
    pod_id: str,
    pod_name: str | None,
    *,
    database_url: str | None = None,
) -> None:
    if not pod_id:
        return
    url = init_db(database_url)
    now = datetime.now(timezone.utc)
    eng = _make_engine(url)
    with Session(eng) as session:
        session.add(
            PodLaunchRecord(
                cluster_name=cluster_name,
                pod_id=pod_id,
                pod_name=pod_name,
                launched_at=now,
            )
        )
        row = session.get(ClusterRecord, cluster_name)
        if row is not None:
            row.updated_at = now
        session.commit()


def get_cluster_record(
    cluster_name: str, *, database_url: str | None = None
) -> dict[str, Any] | None:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ClusterRecord, cluster_name)
        if row is None:
            return None
        return {
            "name": row.name,
            "num_nodes": row.num_nodes,
            "spec_path": row.spec_path,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def delete_cluster_record(cluster_name: str, *, database_url: str | None = None) -> None:
    url = resolve_database_url(database_url)
    if url not in _engines:
        init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ClusterRecord, cluster_name)
        if row is not None:
            session.delete(row)
            session.commit()


def register_serve_daemon(
    cluster_name: str,
    pid: int,
    host: str,
    port: int,
    *,
    spec_path: str | None,
    database_url: str | None = None,
) -> None:
    url = init_db(database_url)
    now = datetime.now(timezone.utc)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ServeDaemonRecord, cluster_name)
        if row is None:
            session.add(
                ServeDaemonRecord(
                    cluster_name=cluster_name,
                    pid=pid,
                    host=host,
                    port=port,
                    spec_path=spec_path,
                    started_at=now,
                )
            )
        else:
            row.pid = pid
            row.host = host
            row.port = port
            if spec_path is not None:
                row.spec_path = spec_path
            row.started_at = now
        session.commit()


def delete_serve_daemon(cluster_name: str, *, database_url: str | None = None) -> None:
    url = resolve_database_url(database_url)
    if url not in _engines:
        init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ServeDaemonRecord, cluster_name)
        if row is not None:
            session.delete(row)
            session.commit()


def get_serve_daemon(
    cluster_name: str, *, database_url: str | None = None
) -> dict[str, Any] | None:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ServeDaemonRecord, cluster_name)
        if row is None:
            return None
        return {
            "cluster_name": row.cluster_name,
            "pid": row.pid,
            "host": row.host,
            "port": row.port,
            "spec_path": row.spec_path,
            "started_at": row.started_at,
        }


def upsert_serve_launch_prefs(
    cluster_name: str,
    *,
    spec_path: str,
    host: str,
    port: int,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    database_url: str | None = None,
) -> None:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ServeLaunchPrefs, cluster_name)
        if row is None:
            session.add(
                ServeLaunchPrefs(
                    cluster_name=cluster_name,
                    spec_path=spec_path,
                    host=host,
                    port=port,
                    reconcile=True,
                    ssl_certfile=ssl_certfile,
                    ssl_keyfile=ssl_keyfile,
                )
            )
        else:
            row.spec_path = spec_path
            row.host = host
            row.port = port
            row.reconcile = True
            row.ssl_certfile = ssl_certfile
            row.ssl_keyfile = ssl_keyfile
        session.commit()


def get_serve_launch_prefs(
    cluster_name: str, *, database_url: str | None = None
) -> dict[str, Any] | None:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ServeLaunchPrefs, cluster_name)
        if row is None:
            return None
        return {
            "cluster_name": row.cluster_name,
            "spec_path": row.spec_path,
            "host": row.host,
            "port": row.port,
            "ssl_certfile": row.ssl_certfile,
            "ssl_keyfile": row.ssl_keyfile,
        }


def list_serve_daemons(*, database_url: str | None = None) -> list[dict[str, Any]]:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        rows = session.scalars(
            select(ServeDaemonRecord).order_by(ServeDaemonRecord.cluster_name)
        ).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "cluster_name": row.cluster_name,
                    "pid": row.pid,
                    "host": row.host,
                    "port": row.port,
                    "spec_path": row.spec_path,
                    "started_at": row.started_at,
                }
            )
        return out


def iter_cluster_names(*, database_url: str | None = None) -> list[str]:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        rows = session.scalars(select(ClusterRecord.name).order_by(ClusterRecord.name))
        return list(rows.all())


def list_clusters_live(
    api_key: str | None,
    *,
    database_url: str | None = None,
) -> list[dict[str, Any]]:
    """Rows for `clusters list`: stored clusters merged with live RunPod status."""
    init_db(database_url)
    names = iter_cluster_names(database_url=database_url)
    out: list[dict[str, Any]] = []
    for name in names:
        row_data = _cluster_row_from_db(name, database_url=database_url)
        pods = list_managed_pods(name, api_key=api_key)
        alive = count_alive_nodes(pods)
        ready = count_ready_nodes(pods)
        out.append(
            {
                "name": name,
                "desired": row_data["num_nodes"],
                "alive": alive,
                "ready": ready,
                "managed_pods": len(pods),
                "spec_path": row_data.get("spec_path"),
                "updated_at": row_data.get("updated_at"),
                "pod_ids": [str(p.get("id")) for p in pods if p.get("id")],
            }
        )
    return out


def _cluster_row_from_db(
    name: str, *, database_url: str | None
) -> dict[str, Any]:
    url = init_db(database_url)
    eng = _make_engine(url)
    with Session(eng) as session:
        row = session.get(ClusterRecord, name)
        if row is None:
            return {"num_nodes": 0, "spec_path": None, "updated_at": None}
        return {
            "num_nodes": row.num_nodes,
            "spec_path": row.spec_path,
            "updated_at": row.updated_at,
        }
