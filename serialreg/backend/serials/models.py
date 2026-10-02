"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）
  Loan         本地流通单据（一次借出）；CirculationEvent 为其事件链（审计）

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有入库 Item 或 Item 丢失。

流通规则：
  只有「未装订且在馆」的实体可借出；借出中不能再借也不能装订；
  已装订实体不能拆成单件外借。借还事件按发生时间排序应用，
  迟到事件只入审计链、不覆盖较新的处置。
"""
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone


class Title(models.Model):
    """连续出版物刊名。"""

    class PublicationStatus(models.TextChoices):
        ACTIVE = "active", "在刊"
        CEASED = "ceased", "停刊"

    title = models.CharField("刊名", max_length=255)
    issn = models.CharField("ISSN", max_length=9, blank=True)
    publisher = models.CharField("出版者", max_length=255, blank=True)
    status = models.CharField(
        "出版状态", max_length=10,
        choices=PublicationStatus.choices, default=PublicationStatus.ACTIVE,
    )
    # 停刊月份：与卷期编号分开记录，只表示出版停止，不改变任何馆藏状态
    ceased_month = models.DateField("停刊月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.status == self.PublicationStatus.CEASED and not self.ceased_month:
            raise ValidationError({"ceased_month": "停刊刊名必须填写停刊月份。"})


class IssueNumber(models.Model):
    """卷期编号（编号槽位），与发行年月解耦。

    跨年卷：同一卷可以跨自然年，例如 v.60 no.3 印的是 2023-12、2024-01，
    编号仍只有一条 (volume=60, number=3)，发行时间记录在 Issue 上。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="numbers",
    )
    volume = models.CharField("卷", max_length=20, blank=True)
    number = models.CharField("期", max_length=20)
    sort_key = models.PositiveIntegerField(
        "排序键", default=0,
        help_text="馆员录入的编号顺序，跨年卷按编号顺序而非月份排列",
    )

    class Meta:
        verbose_name = "期号"
        unique_together = ("title", "volume", "number")
        ordering = ["sort_key", "id"]

    def __str__(self):
        return f"{self.volume}({self.number})" if self.volume else self.number


class Issue(models.Model):
    """一次发行。普通期关联一个 IssueNumber；两期合刊关联两个（或更多）。"""

    class IssueKind(models.TextChoices):
        REGULAR = "regular", "普通期"
        COMBINED = "combined", "合刊"

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="issues",
    )
    kind = models.CharField(
        "类型", max_length=10,
        choices=IssueKind.choices, default=IssueKind.REGULAR,
    )
    # 发行年月与卷期编号分开录入
    issue_month = models.DateField("发行年月", help_text="只取年月；合刊可只填起始月")
    issue_month_end = models.DateField(
        "发行截止年月", null=True, blank=True, help_text="合刊/跨年卷的覆盖结束月",
    )
    numbers = models.ManyToManyField(
        IssueNumber, through="IssueNumbering", related_name="issues",
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "发行期"
        ordering = ["issue_month", "id"]

    def __str__(self):
        nums = "·".join(str(n) for n in self.numbers.all())
        return f"{self.title.title} {nums}"

    def clean(self):
        if self.issue_month_end and self.issue_month_end < self.issue_month:
            raise ValidationError({"issue_month_end": "截止年月不能早于起始年月。"})


class IssueNumbering(models.Model):
    """Issue ↔ IssueNumber 关联表。

    合刊的两个期号必须是两条独立关联记录，而不是把 3-4 塞进一个条码字段。
    """

    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, related_name="numberings",
    )
    number = models.ForeignKey(
        IssueNumber, on_delete=models.CASCADE, related_name="numberings",
    )
    label = models.CharField("封面标识", max_length=40, blank=True,
                             help_text="如 no.3-4，仅作展示")

    class Meta:
        unique_together = ("issue", "number")


class Item(models.Model):
    """馆内实物（册）。一个条码 = 一个实物。"""

    class ItemStatus(models.TextChoices):
        AVAILABLE = "available", "在馆"
        CHECKED_OUT = "checked_out", "借出"
        LOST = "lost", "丢失"
        BOUND = "bound", "已装订"

    barcode = models.CharField("条码", max_length=40, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="items",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="items",
        help_text="实物对应的发行期；合刊实物只指向这一个 Issue，"
                  "对多个期号的覆盖由 IssueNumbering 表达",
    )
    # 未装订时的实际位置；装订后以 binding 的 location 为准
    location = models.CharField("馆藏位置", max_length=100, blank=True)
    status = models.CharField(
        "馆藏状态", max_length=12,
        choices=ItemStatus.choices, default=ItemStatus.AVAILABLE,
    )
    accessioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

    def active_loan(self):
        """当前未关闭的流通单（无则 None）。

        视图里用 Prefetch(..., to_attr="open_loans") 预取时走缓存，避免 N+1。
        """
        prefetched = getattr(self, "open_loans", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.loans.filter(status=Loan.Status.OPEN).first()

    def availability(self):
        """实际可得性：bound / on_loan / lost / available。"""
        if self.is_bound or self.status == self.ItemStatus.BOUND:
            return "bound"
        if self.status == self.ItemStatus.CHECKED_OUT:
            return "on_loan"
        if self.status == self.ItemStatus.LOST:
            return "lost"
        return "available"

    def current_location(self):
        """实际保管位置：装订中→装订册；借出中→流通保管位置；否则自身位置。"""
        entry = getattr(self, "binding_entry", None)
        if entry is not None:
            return entry.binding.location
        loan = self.active_loan()
        if loan is not None:
            return loan.custody_location
        return self.location

    def __str__(self):
        return self.barcode


class Binding(models.Model):
    """装订册：把若干已入藏实物装订在一起，实物身份与条码不变。"""

    call_number = models.CharField("装订索书号", max_length=60, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="bindings",
    )
    location = models.CharField("装订后位置", max_length=100)
    bound_month = models.DateField("装订月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    items = models.ManyToManyField(Item, through="BindingEntry", related_name="bindings")

    class Meta:
        verbose_name = "装订册"
        ordering = ["call_number"]

    def __str__(self):
        return self.call_number


class BindingEntry(models.Model):
    item = models.OneToOneField(
        Item, on_delete=models.CASCADE, related_name="binding_entry",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.CASCADE, related_name="entries",
    )
    # 装订时封存该实物原位置，拆订后恢复
    previous_location = models.CharField("装订前位置", max_length=100, blank=True)
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("item", "binding")

    def clean(self):
        if self.item_id and self.item.title_id != self.binding.title_id:
            raise ValidationError("装订册内的实物必须属于同一种刊。")


class Loan(models.Model):
    """本地流通单据：一次借出。

    一个实物同一时刻最多一张未关闭单据（部分唯一约束保证）。
    单据状态由事件链驱动，不直接编辑：
      open → closed(close_reason=returned)   归还事件
      open → closed(close_reason=lost)      遗失事件
      open（is_overdue=True）               逾期事件，不改变在借事实
    """

    class Status(models.TextChoices):
        OPEN = "open", "在借"
        CLOSED = "closed", "已关闭"

    class CloseReason(models.TextChoices):
        RETURNED = "returned", "已归还"
        LOST = "lost", "遗失"

    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="loans",
    )
    # 幂等标识：同一借出请求重放只会命中同一张单据
    idempotency_key = models.CharField("幂等标识", max_length=64, unique=True)
    due_date = models.DateField("到期日")
    # 借出期间的实际保管位置（流通台/读者处），归还后定位自动回到 Item.location
    custody_location = models.CharField(
        "借出保管位置", max_length=100, default="流通台",
    )
    status = models.CharField(
        "单据状态", max_length=10,
        choices=Status.choices, default=Status.OPEN,
    )
    close_reason = models.CharField(
        "关闭原因", max_length=10,
        choices=CloseReason.choices, null=True, blank=True,
    )
    is_overdue = models.BooleanField("已标记逾期", default=False)
    checked_out_at = models.DateTimeField("借出时间")
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    # 事件链游标：last_seq 为已记录事件序号（含未应用的迟到事件），
    # last_applied_occurred_at 为最近一个「已应用」事件的发生时间，
    # 早于它的迟到事件只入审计链，不覆盖较新处置
    last_seq = models.PositiveIntegerField("事件序号游标", default=0)
    last_applied_occurred_at = models.DateTimeField("最近应用事件时间")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "流通单据"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["item"], condition=Q(status="open"),
                name="unique_open_loan_per_item",
            ),
        ]

    def overdue(self):
        """逾期 = 已标记，或在借且今天已过到期日。"""
        if self.status != self.Status.OPEN:
            return False
        return self.is_overdue or self.due_date < timezone.localdate()

    def __str__(self):
        return f"流通单#{self.pk}({self.item.barcode})"


class CirculationEvent(models.Model):
    """流通事件（审计链）：借出/归还/逾期/遗失。

    每个事件都有幂等标识与单据内发生顺序（seq）：
      - 同一 idempotency_key 重放 → 返回已记录事件，不重复改变状态；
      - occurred_at 早于单据最近已应用事件的 → applied=False，
        仅作审计留痕，不覆盖较新的处置。
    """

    class EventType(models.TextChoices):
        CHECKOUT = "checkout", "借出"
        RETURN = "return", "归还"
        OVERDUE = "overdue", "逾期"
        LOST = "lost", "遗失"

    loan = models.ForeignKey(
        Loan, on_delete=models.CASCADE, related_name="events",
    )
    event_type = models.CharField(
        "事件类型", max_length=10, choices=EventType.choices,
    )
    idempotency_key = models.CharField("幂等标识", max_length=64, unique=True)
    occurred_at = models.DateTimeField("发生时间")
    seq = models.PositiveIntegerField("单据内顺序")
    applied = models.BooleanField("已应用", default=True)
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "流通事件"
        ordering = ["seq", "id"]
        unique_together = ("loan", "seq")

    def __str__(self):
        return f"{self.loan_id}#{self.seq}<{self.event_type}>"


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有在馆实物（含装订）
    issued+missing    已发行但缺藏（无实物或全部丢失/借出按调用方再细分）
    not_published     缺号：没有任何发行记录，不自动等同缺藏
    ceased_gap        停刊后出现的编号（永远不会有发行）
    """
    issues = list(number.issues.prefetch_related("items"))
    if not issues:
        ceased = title.ceased_month
        if title.status == Title.PublicationStatus.CEASED and ceased:
            return "ceased_gap"
        return "not_published"
    items = [it for iss in issues for it in iss.items.all()]
    held = any(it.status != Item.ItemStatus.LOST for it in items)
    return "issued+held" if held else "issued+missing"


def open_loan_prefetch():
    """预取未关闭流通单的 Prefetch，配合 Item.active_loan() 使用。"""
    from django.db.models import Prefetch
    return Prefetch(
        "loans",
        queryset=Loan.objects.filter(status=Loan.Status.OPEN),
        to_attr="open_loans",
    )


def circulation_info(item):
    """实物的流通视图：可得性 + 未关闭单据（到期日/保管位置/逾期）。"""
    loan = item.active_loan()
    if loan is None:
        return None
    return {
        "loan_id": loan.id,
        "due_date": loan.due_date,
        "checked_out_at": loan.checked_out_at,
        "custody_location": loan.custody_location,
        "overdue": loan.overdue(),
    }


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订、借出都可命中）。"""
    from django.db.models import Prefetch
    rows = []
    issues = number.issues.prefetch_related(Prefetch(
        "items",
        queryset=Item.objects.select_related(
            "binding_entry__binding",
        ).prefetch_related(open_loan_prefetch()),
    ))
    for issue in issues:
        for item in issue.items.all():
            rows.append({
                "issue_id": issue.id,
                "barcode": item.barcode,
                "status": item.status,
                "availability": item.availability(),
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
                "circulation": circulation_info(item),
            })
    return rows
