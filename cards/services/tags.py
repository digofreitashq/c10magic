"""
Tags do Scryfall Tagger (https://tagger.scryfall.com).

- Oracle tags: o que a carta faz (removal, ramp, card-advantage...),
  ligadas ao oracle_id.
- Art tags: o que aparece na ilustração (dragon, sword, night...),
  ligadas ao illustration_id.

A API de cartas não traz as tags. O Scryfall publica a lista completa em
/private/tags/{oracle,illustration} (endpoint não documentado, ~80 MB no
total). Ela é baixada no máximo uma vez por semana e guardada em TagIndex.
"""

import logging
import threading
from collections import defaultdict

import requests
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from cards.models import Card, SyncState, TagIndex, TagStat


log = logging.getLogger(__name__)

KINDS = {
    TagIndex.ORACLE: "oracle_ids",
    TagIndex.ART: "illustration_ids",
}


def _state_key(kind):
    return f"tags:{kind}"


def last_sync(kind=TagIndex.ORACLE):
    state = SyncState.objects.filter(key=_state_key(kind)).first()
    return state.updated_at if state else None


def _kind_is_stale(kind, limit):
    synced = last_sync(kind)

    # Sincronizações antigas não guardavam as contagens
    if synced is None or synced < limit or not TagStat.objects.filter(kind=kind).exists():
        return True

    # Sincronização de antes das descrições: refaz uma vez para guardá-las
    return kind == TagIndex.ORACLE and not TagStat.objects.filter(kind=kind).exclude(description="").exists()


def is_stale():
    limit = Card.stale_before()

    return any(_kind_is_stale(kind, limit) for kind in KINDS)


def sync_kind(kind: str, session: requests.Session | None = None) -> int:
    """Baixa as tags de um tipo e reconstrói o índice. Retorna quantos alvos."""

    session = session or requests.Session()

    response = session.get(
        f"{settings.SCRYFALL_API}/private/tags/{kind}",
        headers={"User-Agent": settings.USER_AGENT, "Accept": "application/json"},
        timeout=300,
    )
    response.raise_for_status()

    tags = response.json().get("data", [])
    id_key = KINDS[kind]

    index = defaultdict(set)
    counts = {}
    descriptions = {}

    for tag in tags:
        label = tag.get("label")
        if not label:
            continue
        targets = tag.get(id_key) or []
        counts[label] = len(targets)
        descriptions[label] = (tag.get("description") or "").strip()
        for target_id in targets:
            index[target_id].add(label)

    del tags

    with transaction.atomic():
        TagStat.objects.filter(kind=kind).delete()
        TagStat.objects.bulk_create(
            (TagStat(kind=kind, label=label, count=count, description=descriptions.get(label, ""))
             for label, count in counts.items()),
            batch_size=5000,
        )

        TagIndex.objects.filter(kind=kind).delete()
        TagIndex.objects.bulk_create(
            (
                TagIndex(kind=kind, target_id=target_id, labels="|" + "|".join(sorted(labels)) + "|")
                for target_id, labels in index.items()
            ),
            batch_size=5000,
        )
        SyncState.objects.update_or_create(
            key=_state_key(kind),
            defaults={"updated_at": timezone.now(), "detail": f"{len(index)} itens"},
        )

    return len(index)


def sync_all(force=False) -> dict:
    """Sincroniza os tipos vencidos (ou todos, com force=True)."""

    limit = Card.stale_before()
    session = requests.Session()
    done = {}

    for kind in KINDS:
        if force or _kind_is_stale(kind, limit):
            done[kind] = sync_kind(kind, session)

    return done


class BackgroundTagSync:

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self.last_error = ""

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start_if_stale(self):
        with self._lock:
            if self.running or not is_stale():
                return False

            self._thread = threading.Thread(target=self._run, name="c10magic-tags", daemon=True)
            self._thread.start()

            return True

    def _run(self):
        close_old_connections()

        try:
            sync_all()
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001 - tags são opcionais
            log.exception("Falha ao sincronizar tags do Scryfall")
            self.last_error = str(exc)
        finally:
            close_old_connections()


background_tags = BackgroundTagSync()


def tag_counts(labels, kind=TagIndex.ORACLE) -> dict[str, int]:
    return dict(
        TagStat.objects.filter(kind=kind, label__in=list(labels)).values_list("label", "count")
    )


def tag_descriptions(labels, kind=TagIndex.ORACLE) -> dict[str, str]:
    return dict(
        TagStat.objects.filter(kind=kind, label__in=list(labels)).exclude(description="")
        .values_list("label", "description")
    )


def _humanize(label: str) -> str:
    return label.replace("-", " ")


def tags_for(oracle_id: str = "", illustration_id: str = "") -> dict:
    """
    {"oracle": [(label, texto)], "illustration": [...],
     "oracle_ready": bool, "art_ready": bool}

    *_ready=False enquanto a primeira sincronização daquele tipo não terminou.
    """

    result = {
        TagIndex.ORACLE: [],
        TagIndex.ART: [],
        "oracle_ready": last_sync(TagIndex.ORACLE) is not None,
        "art_ready": last_sync(TagIndex.ART) is not None,
    }

    targets = {TagIndex.ORACLE: oracle_id, TagIndex.ART: illustration_id}

    for row in TagIndex.objects.filter(
        target_id__in=[t for t in targets.values() if t]
    ):
        if targets.get(row.kind) == row.target_id:
            result[row.kind] = [(label, _humanize(label)) for label in row.label_list]

    return result
