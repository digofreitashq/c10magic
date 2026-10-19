from django.urls import path

from cards import views


app_name = "cards"

urlpatterns = [
    path("", views.search, name="search"),
    path("carta/<int:pk>/", views.card_detail, name="detail"),
    path("carta/<int:pk>/atualizar/", views.card_refresh, name="refresh"),
    path("carta/<int:pk>/quantidade/", views.card_quantity, name="quantity"),
    path("sf/<uuid:scryfall_id>/", views.scryfall_detail, name="scryfall_detail"),
    path("sf/<uuid:scryfall_id>/adicionar/", views.scryfall_add, name="scryfall_add"),
    path("sf/<uuid:scryfall_id>/similares/", views.similar, name="similar"),
    # Decks: grade, gerar (construtor), cadastrar e editor
    path("decks/", views.decks, name="decks"),
    path("decks/gerar/", views.builder, name="builder"),
    path("decks/novo/", views.deck_create, name="deck_create"),
    path("decks/<int:pk>/", views.expert_deck, name="expert_deck"),
    path("decks/<int:pk>/importar/", views.deck_import, name="deck_import"),
    path("decks/<int:pk>/renomear/", views.deck_rename, name="deck_rename"),
    path("decks/<int:pk>/clonar/", views.deck_clone, name="deck_clone"),
    path("decks/<int:pk>/capa/", views.deck_cover, name="deck_cover"),
    path("decks/<int:pk>/exportar/<str:fmt>/", views.deck_export, name="deck_export"),
    path("decks/<int:pk>/reconstruir/", views.builder_rebuild, name="builder_rebuild"),
    path("decks/<int:pk>/comandante/", views.expert_commander, name="expert_commander"),
    path("decks/<int:pk>/orcamento/", views.expert_budget, name="expert_budget"),
    path("decks/<int:pk>/excluir/", views.expert_delete, name="expert_delete"),
    path("decks/<int:pk>/carta/<int:card_pk>/sugestoes/", views.expert_suggestions, name="expert_suggestions"),
    path("decks/<int:pk>/carta/<int:card_pk>/trocar/", views.expert_swap, name="expert_swap"),
    path("colecao/", views.manage, name="manage"),
    path("api/status/", views.update_status, name="status"),
    path("api/precos/", views.prices, name="prices"),
    path("api/autocomplete/", views.autocomplete, name="autocomplete"),
]
