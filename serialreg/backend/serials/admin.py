from django.contrib import admin

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item, Title,
)

admin.site.register(Title)
admin.site.register(IssueNumber)
admin.site.register(Issue)
admin.site.register(IssueNumbering)
admin.site.register(Item)
admin.site.register(Binding)
admin.site.register(BindingEntry)
