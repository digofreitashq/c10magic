from django.contrib import admin

from cards.models import BrlPrice, CachedCard, Card, SyncState, TagIndex


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at", "detail")


@admin.register(TagIndex)
class TagIndexAdmin(admin.ModelAdmin):
    list_display = ("kind", "target_id")
    list_filter = ("kind",)
    search_fields = ("target_id", "labels")


@admin.register(CachedCard)
class CachedCardAdmin(admin.ModelAdmin):
    list_display = ("name", "scryfall_id", "updated_at")
    search_fields = ("name",)
    readonly_fields = ("data",)


@admin.register(BrlPrice)
class BrlPriceAdmin(admin.ModelAdmin):
    list_display = ("name", "price_brl", "error", "updated_at")
    search_fields = ("name",)


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = (
        "name", "quantity", "set_code", "rarity", "cmc", "colors",
        "price_brl", "price_usd", "scryfall_updated_at", "ligamagic_updated_at",
    )
    list_filter = ("rarity", "colors", "set_code")
    search_fields = ("name", "type_line", "oracle_text")
    readonly_fields = ("raw_scryfall", "created_at")
