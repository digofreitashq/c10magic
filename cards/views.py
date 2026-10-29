from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from cards.forms import (
    AddCardForm,
    BudgetForm,
    BuilderForm,
    DeckImportForm,
    ImportForm,
    SearchForm,
    SimilarForm,
)
from cards.models import Card, Deck, DeckCard
from cards.search import SCRYFALL_ORDER, QuerySyntaxError, apply_filters, to_scryfall_query
from cards.services.catalog import (
    Collection,
    ScryfallResults,
    from_collection,
    from_scryfall,
    get_card_data,
)
from cards.services.builder import TARGET_KEYS, BuildError
from cards.services.builder import build_deck as build_commander_deck
from cards.services.deck import (
    EXPORT_FORMATS,
    build_deck,
    clone_deck,
    deck_summaries,
    export_deck,
    rename_deck,
    build_pool,
    commander_candidates,
    import_deck,
    set_commanders,
    suggestions_for,
    swap_card,
)
from cards.services.exchange import usd_brl
from cards.services.importer import decode_upload
from cards.services.prices import fetch_price, known_prices, price_queue
from cards.services.scryfall import ScryfallClient, ScryfallQueryError, card_fields
from cards.services.tagpicker import commander_tag_options, creature_type_suggestions, tag_suggestions
from cards.services.similar import SCRYFALL_SORT as SIMILAR_SCRYFALL_SORT
from cards.services.similar import PRIMARY_TYPES, build_reference, collection_queryset
from cards.services.similar import scryfall_query as similar_query
from cards.services.tags import background_tags, tags_for
from cards.services.updater import (
    add_card,
    background,
    clear_collection,
    import_csv_content,
    refresh_card,
    stale_cards,
)
from cards.templatetags.magic import brl


# ============================================================
# BUSCA
# ============================================================

def search(request):
    form = SearchForm(request.GET or None)
    data = form.cleaned_data if form.is_bound and form.is_valid() else {}

    scope = data.get("scope") or "scryfall"
    rate = usd_brl()

    context = {
        "form": form,
        "scope": scope,
        "rate": rate,
        "items": [],
        "page": None,
        "total": 0,
        "totals": {},
        "error": None,
        "warnings": [],
        "searched": True,
        "view_mode": data.get("view") or "grid",
        "advanced_open": form.is_bound and form.is_valid() and form.active_filter_count() > 0,
        "collection_size": Card.objects.count(),
        "updater": background.status(),
    }

    params = request.GET.copy()
    params.pop("page", None)
    context["querystring"] = params.urlencode()

    if scope == "collection":
        _search_collection(request, data, rate, context)
    else:
        _search_scryfall(request, data, rate, context)

    # Preços em reais que ainda não temos: busca em segundo plano
    pending = [item.name for item in context["items"] if item.brl_status == "pending"]
    if pending:
        price_queue.request(pending)

    return render(request, "cards/search.html", context)


def _search_collection(request, data, rate, context):
    cards = Card.objects.all()

    try:
        cards = apply_filters(cards, data)
        total = cards.count()
    except QuerySyntaxError as exc:
        context["error"] = str(exc)
        return

    page = Paginator(cards, settings.CARDS_PER_PAGE).get_page(request.GET.get("page"))

    context.update({
        "page": page,
        "total": total,
        "totals": cards.aggregate(
            copies=Sum("quantity"),
            value_brl=Sum(F("price_brl") * F("quantity")),
        ),
        "items": from_collection(page.object_list, rate),
    })


def _search_scryfall(request, data, rate, context):
    query = to_scryfall_query(data, rate)

    if not query:
        context["searched"] = False
        context["collection_value"] = Card.objects.aggregate(
            value=Sum(F("price_brl") * F("quantity"))
        )["value"]
        return

    sort = data.get("sort") or "name"
    order = data.get("order") or "auto"

    results = ScryfallResults(
        query,
        unique="prints" if data.get("unique") else "cards",
        order=SCRYFALL_ORDER.get(sort, "name"),
        direction=order,
    )

    try:
        total = results.count
        page = Paginator(results, settings.CARDS_PER_PAGE).get_page(request.GET.get("page"))
        items = from_scryfall(list(page.object_list), rate)
    except ScryfallQueryError as exc:
        context["error"] = str(exc)
        return
    except requests.RequestException as exc:
        context["error"] = f"Não foi possível consultar o Scryfall ({exc.__class__.__name__})."
        return

    context.update({
        "page": page,
        "total": total,
        "items": items,
        "warnings": results.warnings,
        "scryfall_query": query,
        "owned_on_page": sum(1 for item in items if item.owned),
    })


# ============================================================
# DETALHE
# ============================================================

def card_detail(request, pk):
    card = get_object_or_404(Card, pk=pk)

    # Primeiro carregamento (ou dados vencidos há mais de uma semana)
    if card.is_stale() and not background.running:
        refresh_card(card)
        card.refresh_from_db()

    others = []
    if card.oracle_id:
        others = Card.objects.filter(oracle_id=card.oracle_id).exclude(pk=card.pk)

    return render(request, "cards/detail.html", {
        "card": card,
        "others": others,
        "back": request.META.get("HTTP_REFERER", ""),
        "rate": usd_brl(),
        **_tags_context(card),
    })


def _tags_context(card):
    # Primeira vez (ou mais de uma semana): baixa as listas em segundo plano
    background_tags.start_if_stale()

    return {
        "tags": tags_for(card.oracle_id, card.illustration_id),
        "tags_syncing": background_tags.running,
    }


def scryfall_detail(request, scryfall_id):
    try:
        data, updated_at = get_card_data(str(scryfall_id))
    except requests.RequestException as exc:
        raise Http404(f"Scryfall indisponível: {exc}") from exc

    if not data:
        raise Http404("Carta não encontrada no Scryfall.")

    # Card não salvo, só para reaproveitar a página de detalhe
    card = Card(name=data["name"], name_normalized=data["name"].lower(), **card_fields(data))
    card.scryfall_updated_at = updated_at

    quote = known_prices([card.name])[card.name]

    if quote is None:
        # Uma carta só: vale esperar ~1 s pela LigaMagic
        try:
            fetch_price(card.name)
            quote = known_prices([card.name])[card.name]
        except requests.RequestException:
            pass

    if quote:
        card.price_brl = quote.price
        card.ligamagic_url = quote.url
        card.ligamagic_updated_at = quote.updated_at
        card.ligamagic_error = quote.error

    owned = Collection([data]).owned(data)

    return render(request, "cards/detail.html", {
        "card": card,
        "owned": owned,
        "owned_quantity": sum(c.quantity for c in owned),
        "scryfall_id": data["id"],
        "back": request.META.get("HTTP_REFERER", ""),
        "rate": usd_brl(),
        **_tags_context(card),
    })


def similar(request, scryfall_id):
    try:
        data, _ = get_card_data(str(scryfall_id))
    except requests.RequestException as exc:
        raise Http404(f"Scryfall indisponível: {exc}") from exc

    if not data:
        raise Http404("Carta não encontrada no Scryfall.")

    background_tags.start_if_stale()

    rate = usd_brl()
    ref = build_reference(data, rate)

    if "s" in request.GET:
        form = SimilarForm(ref, request.GET)
        values = form.cleaned_data if form.is_valid() else ref.defaults()
    else:
        values = ref.defaults()
        form = SimilarForm(ref, initial={**values, "s": "1"})

    params = request.GET.copy()
    for key in ("page", "partial"):
        params.pop(key, None)

    context = {
        "ref": ref,
        "form": form,
        "rate": rate,
        "items": [],
        "page": None,
        "total": 0,
        "error": None,
        "querystring": params.urlencode(),
        "only_owned": values.get("only_owned"),
        "tags_syncing": background_tags.running,
    }

    if values.get("only_owned"):
        cards = collection_queryset(ref, values)
        page = Paginator(cards, settings.CARDS_PER_PAGE).get_page(request.GET.get("page"))
        context.update({
            "page": page,
            "total": page.paginator.count,
            "items": from_collection(page.object_list, rate),
        })
    else:
        query = similar_query(ref, values, rate)
        order, direction = SIMILAR_SCRYFALL_SORT[values.get("sort") or "edhrec"]

        results = ScryfallResults(query, order=order, direction=direction)

        try:
            page = Paginator(results, settings.CARDS_PER_PAGE).get_page(request.GET.get("page"))
            context.update({
                "page": page,
                "total": results.count,
                "items": from_scryfall(list(page.object_list), rate),
                "warnings": results.warnings,
            })
        except ScryfallQueryError as exc:
            context["error"] = str(exc)
        except requests.RequestException as exc:
            context["error"] = f"Não foi possível consultar o Scryfall ({exc.__class__.__name__})."

        context["scryfall_query"] = query

    pending = [item.name for item in context["items"] if item.brl_status == "pending"]
    if pending:
        price_queue.request(pending)

    template = "cards/_similar_results.html" if request.GET.get("partial") else "cards/similar.html"

    return render(request, template, context)


@require_POST
def scryfall_add(request, scryfall_id):
    data, updated_at = get_card_data(str(scryfall_id))

    if not data:
        raise Http404("Carta não encontrada no Scryfall.")

    try:
        quantity = max(1, int(request.POST.get("quantity", 1)))
    except ValueError:
        quantity = 1

    owned = Collection([data]).owned(data)

    if owned:
        card = owned[0]
        card.quantity += quantity
        card.save(update_fields=["quantity"])
        messages.success(request, f"{card.name}: quantidade = {card.quantity}.")
        return redirect("cards:detail", pk=card.pk)

    card, _ = add_card(data["name"], quantity, data.get("set", ""), data.get("collector_number", ""))

    # Já temos os dados do Scryfall: não precisa buscar de novo
    for name, value in card_fields(data).items():
        setattr(card, name, value)
    card.scryfall_updated_at = updated_at
    card.scryfall_error = ""

    quote = known_prices([card.name])[card.name]
    if quote:
        card.price_brl = quote.price
        card.ligamagic_url = quote.url
        card.ligamagic_updated_at = quote.updated_at
        card.ligamagic_error = quote.error

    card.save()

    messages.success(request, f"{card.name} adicionada à coleção (quantidade {card.quantity}).")

    return redirect("cards:detail", pk=card.pk)


@require_POST
def card_refresh(request, pk):
    card = get_object_or_404(Card, pk=pk)

    done = refresh_card(card)

    if any(done.values()):
        messages.success(request, f"{card.name}: dados atualizados.")
    else:
        messages.info(
            request,
            f"{card.name} foi atualizada há menos de "
            f"{settings.CARD_REFRESH_DAYS} dias. Próxima atualização em "
            f"{timezone.localtime(card.next_refresh_at()):%d/%m/%Y %H:%M}.",
        )

    return redirect("cards:detail", pk=card.pk)


@require_POST
def card_quantity(request, pk):
    card = get_object_or_404(Card, pk=pk)

    try:
        quantity = int(request.POST.get("quantity", card.quantity))
    except ValueError:
        quantity = card.quantity

    if quantity <= 0:
        name = card.name
        card.delete()
        messages.success(request, f"{name} removida da coleção.")
        return redirect(reverse("cards:search") + "?scope=collection")

    card.quantity = quantity
    card.save(update_fields=["quantity"])

    messages.success(request, f"{card.name}: quantidade = {quantity}.")

    return redirect("cards:detail", pk=card.pk)


# ============================================================
# ESPECIALISTA
# ============================================================

RADAR_AXES = [
    ("synergy", "Sinergia"),
    ("removal", "Remoção"),
    ("draw", "Compra de cartas"),
    ("sweeper", "Board wipe"),
    ("ramp", "Ramp"),
    ("creatures", "Criaturas"),
]
ROLE_LABEL_KEYS = {"Remoção": "removal", "Compra de cartas": "draw", "Board wipe": "sweeper", "Ramp": "ramp"}


def radar_data(report):
    """Séries do gráfico de radar do relatório: deck atual, perfil e alvo do usuário."""

    if not report:
        return None

    roles = {ROLE_LABEL_KEYS.get(r["label"]): r for r in report.get("roles", [])}
    current = {key: roles[key]["count"] for key in ROLE_LABEL_KEYS.values() if key in roles}
    current["synergy"] = report.get("synergy_slots") or 0
    current["creatures"] = report.get("creatures") or 0

    targets = {key: roles[key]["target"] for key in ROLE_LABEL_KEYS.values() if key in roles}
    targets["synergy"] = max(report.get("slots", 0) - sum(targets.values()), 0)
    targets["creatures"] = report.get("creature_target") or current["creatures"]

    profile = report.get("profile_targets") or targets
    profile = {key: profile.get(key) if profile.get(key) is not None else targets[key] for key in targets}

    return {
        "slots": report.get("slots", 59),
        "axes": [{"key": key, "label": label} for key, label in RADAR_AXES],
        "series": {
            "current": current,
            "profile": profile,
            "target": report.get("overrides") or targets,
        },
        "custom": bool(report.get("overrides")),
    }


def _redirect_back(request, default):
    """Volta para `next` (se for deste site) ou para `default`."""

    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(default)


def _import_errors(form, text, deck=None):
    try:
        return import_deck(text, deck=deck)
    except ValueError as exc:
        form.add_error("decklist", str(exc))
    except requests.RequestException as exc:
        form.add_error("decklist", f"Não foi possível consultar o Scryfall ({exc.__class__.__name__}).")
    return None


def decks(request):
    """Grade de decks (capa, nome, cores e datas)."""

    return render(request, "cards/decks.html", {
        "summaries": deck_summaries(Deck.objects.prefetch_related("cards")),
        "export_formats": EXPORT_FORMATS,
    })


@require_POST
def deck_create(request):
    """Cadastrar deck: cria um deck vazio e abre no editor para colar a lista."""

    deck = Deck.objects.create(name="Novo deck", name_locked=True, raw_text="")
    return redirect("cards:expert_deck", pk=deck.pk)


@require_POST
def deck_import(request, pk):
    """Carrega (ou substitui) a lista de cartas de um deck existente."""

    deck = get_object_or_404(Deck, pk=pk)
    form = DeckImportForm(request.POST)

    if form.is_valid() and _import_errors(form, form.cleaned_data["decklist"], deck=deck):
        messages.success(request, "Lista importada.")
    else:
        for error in form.errors.get("decklist", []):
            messages.error(request, error)

    return redirect("cards:expert_deck", pk=deck.pk)


@require_POST
def deck_rename(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    rename_deck(deck, request.POST.get("name", ""))
    messages.success(request, f"Deck renomeado para {deck.name}.")
    return _redirect_back(request, reverse("cards:expert_deck", args=[deck.pk]))


@require_POST
def deck_clone(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    copy = clone_deck(deck)
    messages.success(request, f"Deck clonado: {copy.name}.")
    return redirect("cards:expert_deck", pk=copy.pk)


@require_POST
def deck_cover(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    scryfall_id = request.POST.get("scryfall_id", "")

    if scryfall_id and not deck.cards.filter(scryfall_id=scryfall_id).exists():
        messages.error(request, "Essa carta não está no deck.")
    else:
        deck.cover_scryfall_id = scryfall_id
        deck.save(update_fields=["cover_scryfall_id", "updated_at"])
        messages.success(request, "Capa do deck atualizada.")

    return _redirect_back(request, reverse("cards:expert_deck", args=[deck.pk]))


@require_GET
def deck_export(request, pk, fmt):
    deck = get_object_or_404(Deck, pk=pk)

    if fmt not in EXPORT_FORMATS:
        raise Http404("Formato desconhecido.")

    response = HttpResponse(export_deck(deck, fmt), content_type="text/plain; charset=utf-8")
    filename = slugify(deck.name or f"deck-{deck.pk}") or f"deck-{deck.pk}"
    if request.GET.get("download"):
        response["Content-Disposition"] = f'attachment; filename="{filename}-{fmt}.txt"'
    return response


def expert_deck(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    rate = usd_brl()

    try:
        view = build_deck(deck, rate)
    except requests.RequestException as exc:
        messages.error(request, f"Não foi possível consultar o Scryfall ({exc.__class__.__name__}).")
        return redirect("cards:decks")

    context = {
        "deck": deck,
        "view": view,
        "rate": rate,
        "budget_form": BudgetForm(initial={"budget": deck.budget}),
        "just_built": bool(request.GET.get("construido")),
        "import_form": DeckImportForm(initial={"decklist": deck.raw_text}),
        "export_formats": EXPORT_FORMATS,
        "cover": deck_summaries([deck])[0]["cover"],
        "radar": radar_data(deck.build_report),
        "open_report": bool(request.GET.get("relatorio")),
        "wanted_value": ",".join((deck.build_report or {}).get("wanted_tags") or []),
        "wanted_commanders": "|".join((deck.build_report or {}).get("commanders") or []),
    }

    if not view.items:
        # Deck recém-cadastrado: ainda sem cartas
        context["empty"] = True
        return render(request, "cards/expert_deck.html", context)

    if not view.commanders:
        # Não deu para saber quem é o comandante: o usuário escolhe
        candidates = commander_candidates(view)
        context.update({
            "choose_commander": True,
            "candidates": candidates,
            "others": [i for i in view.items if i.data and i not in candidates],
        })
        return render(request, "cards/expert_deck.html", context)

    deck_pool = build_pool(view, rate)
    context["pool"] = deck_pool

    pending = [i.result.name for i in view.items if i.result and i.result.brl_status == "pending"]
    pending += [p.result.name for p in deck_pool.by_id.values() if p.result.brl_status == "pending"]
    if pending:
        price_queue.request(pending)

    return render(request, "cards/expert_deck.html", context)


@require_POST
def expert_commander(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    ids = request.POST.getlist("commander")

    if not ids:
        messages.error(request, "Escolha o comandante.")
    elif len(ids) > 2:
        messages.error(request, "Escolha no máximo dois comandantes (partner / background).")
    else:
        set_commanders(deck, ids)

    return redirect("cards:expert_deck", pk=deck.pk)


@require_POST
def expert_budget(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    form = BudgetForm(request.POST)

    if form.is_valid():
        deck.budget = form.cleaned_data["budget"]
        deck.save(update_fields=["budget", "updated_at"])
    else:
        messages.error(request, "Orçamento inválido.")

    return _redirect_back(request, reverse("cards:expert_deck", args=[deck.pk]))


@require_GET
def expert_suggestions(request, pk, card_pk):
    deck = get_object_or_404(Deck, pk=pk)
    deck_card = get_object_or_404(DeckCard, pk=card_pk, deck=deck)
    rate = usd_brl()

    view = build_deck(deck, rate)
    deck_pool = build_pool(view, rate)

    try:
        user_max = Decimal(request.GET.get("max_price", "").replace(",", "."))
        user_max = user_max if user_max >= 0 else None
    except InvalidOperation:
        user_max = None

    # "tags_set" distingue "desmarcou tudo" de "ainda não escolheu" (padrão)
    chosen = bool(request.GET.get("tags_set"))
    user_tags = request.GET.getlist("tags") if chosen else None
    user_types = request.GET.getlist("types") if chosen else None

    suggestions = suggestions_for(
        view, deck_pool, deck_card, rate,
        user_max_price=user_max, user_tags=user_tags, user_types=user_types,
    )

    pending = [s.result.name for s in suggestions.pool_items + suggestions.other_items
               if s.result.brl_status == "pending"]
    if pending:
        price_queue.request(pending)

    return render(request, "cards/_expert_suggestions.html", {
        "deck": deck,
        "view": view,
        "s": suggestions,
        "rate": rate,
        "card_types": PRIMARY_TYPES,
    })


@require_POST
def expert_swap(request, pk, card_pk):
    deck = get_object_or_404(Deck, pk=pk)
    deck_card = get_object_or_404(DeckCard, pk=card_pk, deck=deck)
    old_name = deck_card.name or deck_card.input_name

    try:
        data = swap_card(deck_card, request.POST.get("scryfall_id", ""))
    except (ValueError, requests.RequestException) as exc:
        messages.error(request, f"Não foi possível trocar a carta: {exc}")
    else:
        messages.success(request, f"{old_name} → {data['name']}")

    return redirect(reverse("cards:expert_deck", args=[deck.pk]) + f"#card-{deck_card.pk}")


# ============================================================
# CONSTRUTOR
# ============================================================

def builder(request):
    form = BuilderForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        names = [data["commander"], data.get("partner") or ""]

        try:
            deck = build_commander_deck(names, data.get("budget"), usd_brl(),
                                        priority_tags=data.get("priority_tags"))
        except BuildError as exc:
            form.add_error(None, str(exc))
        except requests.RequestException as exc:
            form.add_error(None, f"Não foi possível consultar o Scryfall ({exc.__class__.__name__}).")
        else:
            return redirect(reverse("cards:expert_deck", args=[deck.pk]) + "?construido=1")

    background_tags.start_if_stale()

    return render(request, "cards/builder.html", {"form": form})


def _target_overrides(request, report):
    """
    Alvos do gráfico (target_<eixo>) se vieram no POST; "reset" volta ao
    perfil; senão, mantém os que o deck já tinha.
    """

    if request.POST.get("reset"):
        return None

    posted = {key: request.POST.get(f"target_{key}") for key in TARGET_KEYS}
    if any(value not in (None, "") for value in posted.values()):
        overrides = {}
        for key, value in posted.items():
            try:
                overrides[key] = max(int(float(value)), 0)
            except (TypeError, ValueError):
                overrides[key] = 0
        return overrides

    return report.get("overrides")


def _priority_tags(request, report):
    """Tags do relatório (campo priority_tags, mesmo vazio); sem o campo, mantém as do deck."""

    if "priority_tags" in request.POST:
        return [t.strip() for t in request.POST["priority_tags"].split(",") if t.strip()]

    return report.get("wanted_tags")


@require_POST
def builder_rebuild(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    report = deck.build_report or {}
    from_report = bool(request.POST.get("open_report"))

    try:
        build_commander_deck(report.get("commanders") or [c.name for c in deck.commanders],
                             deck.budget, usd_brl(), deck=deck,
                             overrides=_target_overrides(request, report),
                             priority_tags=_priority_tags(request, report))
    except (BuildError, requests.RequestException) as exc:
        messages.error(request, f"Não foi possível reconstruir: {exc}")
    else:
        deck.refresh_from_db()
        wanted = (deck.build_report or {}).get("wanted") or []
        summary = "; ".join(f"{w['tag']}: {w['in_deck']} de {w['candidates']} cartas" for w in wanted)
        messages.success(
            request,
            "Deck gerado de novo" + (f". Tags: {summary}." if summary else " com os preços atuais."),
        )

    suffix = "?relatorio=1" if from_report else "?construido=1"
    return redirect(reverse("cards:expert_deck", args=[deck.pk]) + suffix)


@require_POST
def expert_delete(request, pk):
    deck = get_object_or_404(Deck, pk=pk)
    name = str(deck)
    deck.delete()
    messages.success(request, f"Deck {name} excluído.")

    return _redirect_back(request, reverse("cards:decks"))


# ============================================================
# COLEÇÃO
# ============================================================

def manage(request):
    import_form = ImportForm()
    add_form = AddCardForm()

    if request.method == "POST" and request.POST.get("action") == "import":
        import_form = ImportForm(request.POST, request.FILES)

        if import_form.is_valid():
            rows = created = 0

            for upload in import_form.cleaned_data["files"]:
                try:
                    result = import_csv_content(decode_upload(upload.read()))
                except ValueError as exc:
                    messages.error(request, f"{upload.name}: {exc}")
                    continue

                rows += result["rows"]
                created += result["created"]

            if rows:
                messages.success(
                    request,
                    f"{rows} linhas importadas, {created} cartas novas. "
                    "Os dados estão sendo carregados em segundo plano.",
                )
                background.start()

            return redirect("cards:manage")

    elif request.method == "POST" and request.POST.get("action") == "add":
        add_form = AddCardForm(request.POST)

        if add_form.is_valid():
            card, created = add_card(
                add_form.cleaned_data["name"], add_form.cleaned_data["quantity"]
            )
            refresh_card(card)
            card.refresh_from_db()

            if card.scryfall_error and created:
                messages.warning(request, f"{card.name}: {card.scryfall_error}")
            else:
                messages.success(
                    request,
                    f"{card.name} {'adicionada' if created else 'atualizada'} "
                    f"(quantidade {card.quantity}).",
                )

            return redirect("cards:detail", pk=card.pk)

    elif request.method == "POST" and request.POST.get("action") == "update":
        if background.start():
            messages.success(request, "Atualização iniciada em segundo plano.")
        else:
            messages.info(request, "A atualização já está em andamento.")

        return redirect("cards:manage")

    elif request.method == "POST" and request.POST.get("action") == "stop":
        background.stop()
        messages.info(request, "A atualização vai parar após a carta atual.")
        return redirect("cards:manage")

    elif request.method == "POST" and request.POST.get("action") == "clear":
        if request.POST.get("confirm", "").strip() != "LIMPAR":
            messages.error(request, 'Digite LIMPAR para confirmar a limpeza da coleção.')
            return redirect("cards:manage")

        background.stop()
        removed = clear_collection()
        messages.success(request, f"Coleção limpa: {removed} cartas removidas.")
        return redirect("cards:manage")

    stats = Card.objects.aggregate(
        cards=Count("id"),
        copies=Sum("quantity"),
        value_brl=Sum(F("price_brl") * F("quantity")),
        value_usd=Sum(F("price_usd") * F("quantity")),
    )

    return render(request, "cards/manage.html", {
        "import_form": import_form,
        "add_form": add_form,
        "stats": stats,
        "updater": background.status(),
        "refresh_days": settings.CARD_REFRESH_DAYS,
        "errors": Card.objects.exclude(scryfall_error="", ligamagic_error="")[:50],
        "most_valuable": Card.objects.filter(price_brl__isnull=False).order_by("-price_brl")[:12],
        "pending_preview": stale_cards()[:10],
    })


# ============================================================
# API
# ============================================================

@require_GET
def update_status(request):
    status = background.status()
    status["price_queue"] = price_queue.size()
    return JsonResponse(status)


@require_GET
def prices(request):
    """Preços em reais já conhecidos; os que faltam entram na fila."""

    names = request.GET.getlist("n")[:200]
    quotes = known_prices(names)

    missing = [name for name, quote in quotes.items() if quote is None]
    if missing:
        price_queue.request(missing)

    result = {}

    for name, quote in quotes.items():
        if quote is None:
            status = "failed" if price_queue.has_failed(name) else "pending"
            result[name] = {"status": status}
        elif quote.price is None:
            result[name] = {"status": "none", "url": quote.url}
        else:
            result[name] = {
                "status": "ok",
                "brl": str(quote.price),
                "display": brl(quote.price),
                "url": quote.url,
            }

    return JsonResponse({"prices": result, "queue": price_queue.size()})


@require_GET
def commander_tags(request):
    """Tags do comandante (e do segundo), para o usuário escolher quais valorizar."""

    names = [request.GET.get("name", ""), request.GET.get("partner", "")]
    names = [n.strip() for n in names if n.strip()]

    if not names:
        return JsonResponse({"commanders": [], "tags": [], "ready": True})

    try:
        return JsonResponse(commander_tag_options(names))
    except LookupError as exc:
        return JsonResponse({"error": str(exc), "tags": []}, status=404)
    except requests.RequestException:
        return JsonResponse({"error": "Não foi possível consultar o Scryfall.", "tags": []}, status=502)


@require_GET
def tags_autocomplete(request):
    return JsonResponse({"data": tag_suggestions(request.GET.get("q", ""))})


@require_GET
def creature_types_autocomplete(request):
    try:
        data = creature_type_suggestions(request.GET.get("q", ""))
    except requests.RequestException:
        data = []

    return JsonResponse({"data": data})


@require_GET
def autocomplete(request):
    query = request.GET.get("q", "").strip()

    if len(query) < 2:
        return JsonResponse({"data": []})

    local = list(
        Card.objects.filter(name__icontains=query).values_list("name", flat=True)[:10]
    )

    try:
        remote = ScryfallClient(delay=0).autocomplete(query)
    except Exception:  # noqa: BLE001 - sugestão é opcional
        remote = []

    seen = set()
    names = []

    for name in local + remote:
        if name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)

    return JsonResponse({"data": names[:20]})
