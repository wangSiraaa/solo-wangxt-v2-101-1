"""装入验证样例：跨年卷、停刊月份、两期合刊，以及一次装订/拆订演示。"""
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item, Title,
)


class Command(BaseCommand):
    help = "创建跨年卷、停刊、两期合刊样例数据（幂等，可重复执行）"

    @transaction.atomic
    def handle(self, *args, **options):
        # 1) 跨年卷：《年鉴研究》v.60 跨 2023→2024
        t1, _ = Title.objects.get_or_create(
            issn="1001-0001",
            defaults={"title": "年鉴研究", "publisher": "年鉴出版社"},
        )
        n1 = self._number(t1, "60", "1", 1)
        n2 = self._number(t1, "60", "2", 2)
        n3 = self._number(t1, "60", "3", 3)
        i1 = self._issue(t1, "regular", date(2023, 10, 1), None, [n1])
        # v.60 no.3：一个编号，发行覆盖 2023-12 ~ 2024-01
        i3 = self._issue(t1, "regular", date(2023, 12, 1), date(2024, 1, 1), [n3])
        self._item("NJ-60-1", t1, i1, "现刊区 A-01")
        self._item("NJ-60-3", t1, i3, "现刊区 A-01")
        self.stdout.write(self.style.SUCCESS(
            f"✓ 跨年卷《{t1.title}》：v.60 no.3 发行 {i3.issue_month:%Y-%m} ~ "
            f"{i3.issue_month_end:%Y-%m}；no.2 为缺号（无发行记录）"))

        # 2) 停刊：《读者月报》2024-06 停刊，no.5 已发行缺藏，no.6 缺号
        t2, _ = Title.objects.get_or_create(
            issn="2002-0002",
            defaults={
                "title": "读者月报", "publisher": "月报出版有限公司",
                "status": Title.PublicationStatus.CEASED,
                "ceased_month": date(2024, 6, 1),
            },
        )
        m5 = self._number(t2, "12", "5", 5)
        self._number(t2, "12", "6", 6)
        i5 = self._issue(t2, "regular", date(2024, 5, 1), None, [m5])
        self.stdout.write(self.style.SUCCESS(
            f"✓ 停刊《{t2.title}》：停刊月 {t2.ceased_month:%Y-%m}；"
            f"no.5 已发行{'' if i5.items.exists() else '但未入藏（缺藏）'}；"
            f"no.6 无发行记录（缺号，不自动等同缺藏）"))

        # 3) 两期合刊：《双月评论》v.8 no.3-4
        t3, _ = Title.objects.get_or_create(
            issn="3003-0003",
            defaults={"title": "双月评论", "publisher": "评论杂志社"},
        )
        c3 = self._number(t3, "8", "3", 3)
        c4 = self._number(t3, "8", "4", 4)
        c5 = self._number(t3, "8", "5", 5)
        comb = self._issue(t3, "combined", date(2024, 3, 1),
                           date(2024, 4, 1), [c3, c4], label="no.3-4")
        self._item("SY-8-34", t3, comb, "现刊区 B-02")
        i5c = self._issue(t3, "regular", date(2024, 5, 1), None, [c5])
        self._item("SY-8-5", t3, i5c, "现刊区 B-02")
        self.stdout.write(self.style.SUCCESS(
            f"✓ 合刊《{t3.title}》：一条合刊发行关联 "
            f"{list(comb.numbers.values_list('volume', 'number'))}，"
            f"实物 SY-8-34 可从 no.3 或 no.4 命中"))

        # 4) 装订演示：把合刊册与 no.5 装订在一起
        binding, created = Binding.objects.get_or_create(
            call_number="Q/SY-2024",
            defaults={"title": t3, "location": "装订库 C-12",
                      "bound_month": date(2024, 8, 1)},
        )
        if created:
            for barcode, prev in (("SY-8-34", "现刊区 B-02"),
                                  ("SY-8-5", "现刊区 B-02")):
                it = Item.objects.get(barcode=barcode)
                BindingEntry.objects.create(
                    item=it, binding=binding, previous_location=prev)
                it.status = Item.ItemStatus.BOUND
                it.save(update_fields=["status"])
            self.stdout.write(self.style.SUCCESS(
                "✓ 装订册 Q/SY-2024：SY-8-34 + SY-8-5 → 装订库 C-12；"
                "可调用 /api/bindings/unbind/ 拆订恢复原位置"))

    def _number(self, title, volume, number, sort_key):
        obj, _ = IssueNumber.objects.get_or_create(
            title=title, volume=volume, number=number,
            defaults={"sort_key": sort_key},
        )
        return obj

    def _issue(self, title, kind, start, end, numbers, label=""):
        issue = Issue.objects.filter(
            title=title, numberings__number__in=numbers,
        ).distinct().first()
        if issue is None:
            issue = Issue.objects.create(
                title=title, kind=kind, issue_month=start,
                issue_month_end=end,
            )
            issue.numbers.set(numbers)
            if label:
                issue.numberings.update(label=label)
        return issue

    def _item(self, barcode, title, issue, location):
        Item.objects.get_or_create(
            barcode=barcode,
            defaults={"title": title, "issue": issue, "location": location},
        )
