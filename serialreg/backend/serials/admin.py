from django.contrib import admin

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering,
    Item, Loan, LoanEvent, Title,
)

admin.site.register(Title)
admin.site.register(IssueNumber)
admin.site.register(Issue)
admin.site.register(IssueNumbering)
admin.site.register(Item)
admin.site.register(Binding)
admin.site.register(BindingEntry)


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = (
        "id", "item", "status", "borrower", "checkout_at", "due_at",
        "return_at", "lost_at", "version",
    )
    list_filter = ("status",)
    search_fields = ("item__barcode", "borrower")
    raw_id_fields = ("item",)


@admin.register(LoanEvent)
class LoanEventAdmin(admin.ModelAdmin):
    list_display = (
        "loan", "seq", "type", "occurred_at", "recorded_at",
        "applied", "superseded",
    )
    list_filter = ("type", "applied", "superseded")
    search_fields = ("event_id", "item__barcode", "actor")
    raw_id_fields = ("loan", "item")
