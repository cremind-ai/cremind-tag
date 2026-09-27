"""`cremind-tag queue` — inspect and manage the local durable delivery queue.

::

    cremind-tag queue list [--all] [--tag 1A2B3C4D]      # jobs on tags / not yet reported
    cremind-tag queue show 501                           # one job, the screens that carried it
    cremind-tag queue retry 501 | --tag 1A2B3C4D         # unblock the tag, send now, compose afresh
    cremind-tag queue cancel 501                         # drop the card; Cremind is told `cancelled`
    cremind-tag queue purge [--older-than 7d]            # delete finished history

The daemon picks up changes made here within ``daemon.scan_interval_s``. Card
titles are shown (they are what the tag shows); nothing here prints a secret.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from cremind_tag.cli._hardware import JSON_HELP, console, fail, open_db, print_json, table

if TYPE_CHECKING:
    from cremind_tag.daemon.store import JobRow, RevisionRow

app = typer.Typer(name="queue", help="Inspect and manage the local durable delivery queue.", no_args_is_help=True)


def _store(db: Any) -> Any:
    from cremind_tag.daemon.store import QueueStore

    return QueueStore(db)


def _tag(text: str) -> int:
    from cremind_tag.enroll.hardware import parse_tag_id

    try:
        return parse_tag_id(text)
    except ValueError as exc:
        fail(str(exc))


def _job(job: JobRow) -> dict[str, Any]:
    return {"delivery_id": job.delivery_id, "tag_id": f"{job.tag_id:08X}", "kind": job.kind, "priority": job.priority,
            "title": job.title, "state": job.state, "last_stage": job.last_stage, "outcome": job.outcome,
            "revision": job.revision, "epoch": job.epoch, "created_at": job.created_at, "expires_at": job.expires_at,
            "detail": job.detail, "profile": job.profile, "credential_id": job.credential_id}


def _revision(rev: RevisionRow) -> dict[str, Any]:
    return {"tag_id": f"{rev.tag_id:08X}", "revision": rev.revision, "state": rev.state, "purpose": rev.purpose,
            "epoch": rev.epoch, "bridge_addr": rev.bridge_addr, "delivery_ids": list(rev.delivery_ids),
            "pending_delivery_ids": list(rev.pending_delivery_ids), "layout_bytes": len(rev.layout),
            "layout_digest": rev.layout_digest, "frame_digest": rev.frame_digest, "attempts": rev.attempts,
            "last_status": rev.last_status, "detail": rev.detail, "created_at": rev.created_at,
            "sent_at": rev.sent_at, "finished_at": rev.finished_at}


@app.command("list")
def list_jobs(include_all: bool = typer.Option(False, "--all", help="Include finished jobs."),
              tag: str | None = typer.Option(None, "--tag", help="Only this tag (8 hex digits)."),
              limit: int = typer.Option(100, "--limit", min=1, max=10000),
              as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """Jobs in the local queue, newest first."""
    with open_db() as db:
        store = _store(db)
        jobs = [_job(j) for j in store.list_jobs(include_finished=include_all,
                                                 tag_id=_tag(tag) if tag else None, limit=limit)]
        stats = store.queue_stats()
    if as_json:
        print_json({"jobs": jobs, "stats": stats})
        return
    t = table("Queue", "Delivery", "Tag", "Kind", "Title", "State", "Stage / outcome", "Expires")
    for j in jobs:
        title = j["title"] if len(j["title"]) <= 40 else j["title"][:39] + "…"
        t.add_row(str(j["delivery_id"]), j["tag_id"], j["kind"], title, j["state"],
                  j["outcome"] or j["last_stage"] or "-", j["expires_at"])
    console.print(t if jobs else "The queue is empty.")
    console.print(f"{stats['depth']} waiting (oldest {stats['oldest_age_s']} s); revisions {stats['revisions']}; "
                  f"outbox {stats['outbox']}")
    for hw, reason in stats["blocked"].items():
        console.print(f"tag {hw}: [red]blocked[/red] ({reason})")


@app.command()
def show(delivery_id: int = typer.Argument(..., help="Cremind delivery id."),
         as_json: bool = typer.Option(False, "--json", help=JSON_HELP)) -> None:
    """One job and every screen revision that carried it."""
    with open_db() as db:
        store = _store(db)
        job = store.get_job(delivery_id)
        if job is None:
            fail(f"no job {delivery_id} in the local queue")
        data: dict[str, Any] = {"job": {**_job(job), "card": job.card},
                                "revisions": [_revision(r) for r in store.revisions_with(delivery_id)]}
    if as_json:
        print_json(data)
        return
    t = table(f"Job {delivery_id}", "Field", "Value")
    for key, value in data["job"].items():
        if key != "card":
            t.add_row(key, "-" if value is None else str(value))
    console.print(t)
    r = table("Revisions", "Tag", "Revision", "Purpose", "State", "Attempts", "Last status", "Shown / footer")
    for rev in data["revisions"]:
        r.add_row(rev["tag_id"], str(rev["revision"]), rev["purpose"], rev["state"], str(rev["attempts"]),
                  rev["last_status"] or "-", f"{len(rev['delivery_ids'])} / {len(rev['pending_delivery_ids'])}")
    console.print(r if data["revisions"] else "Not on any screen yet.")


@app.command()
def retry(delivery_ids: list[int] | None = typer.Argument(None, help="Delivery ids (their tags are retried)."),
          tag: list[str] = typer.Option([], "--tag", help="Tag id (8 hex digits); repeatable.")) -> None:
    """Unblock tags, send their pending screens now and compose them afresh."""
    if not delivery_ids and not tag:
        fail("name delivery ids or --tag")
    with open_db() as db:
        tags = _store(db).retry(delivery_ids=delivery_ids or [], tag_ids=[_tag(t) for t in tag])
    if not tags:
        fail("nothing to retry (unknown delivery ids)")
    console.print("retrying tag(s) " + ", ".join(f"{t:08X}" for t in tags) + " — the running daemon picks it up")


@app.command()
def cancel(delivery_id: int = typer.Argument(..., help="Cremind delivery id."),
           yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask.")) -> None:
    """Drop a card from its tag; Cremind is told the delivery was cancelled."""
    if not yes and not typer.confirm(f"Cancel delivery {delivery_id}?", default=False):
        raise typer.Exit(1)
    with open_db() as db:
        job = _store(db).cancel_job(delivery_id)
    if job is None:
        fail(f"no job {delivery_id} in the local queue")
    console.print(f"delivery {delivery_id}: {job.state} ({job.outcome})")


def _duration(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    value = text.strip().lower()
    try:
        if value and value[-1] in units:
            return float(value[:-1]) * units[value[-1]]
        return float(value) * 86400
    except ValueError:
        fail(f"not a duration: {text!r} (e.g. 7d, 12h)")


@app.command()
def purge(older_than: str = typer.Option("7d", "--older-than", help="Age of finished history to delete "
                                                                   "(7d, 12h, 30m)."),
          yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask.")) -> None:
    """Delete finished jobs, old revisions and dead outbox rows (never cards still on a tag)."""
    seconds = _duration(older_than)
    if not yes and not typer.confirm(f"Delete finished queue history older than {older_than}?", default=False):
        raise typer.Exit(1)
    with open_db() as db:
        counts = _store(db).purge(older_than_s=seconds)
    console.print("purged " + ", ".join(f"{v} {k}" for k, v in counts.items()))
