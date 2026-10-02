"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

流通层（事件溯源）：
  Loan         本地流通单据（一张借出单），状态由 LoanEvent 链物化而来
  LoanEvent    借/还/遗失事件，仅追加；带幂等标识、单内顺序号与业务发生时间。
               逾期不入库为事件，而是 Loan 上按 due_at 派生的只读状态。

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有入库 Item 或 Item 丢失。
"""
from django.db import models
from django.db.models import Q, Prefetch
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

    @property
    def has_active_loan(self):
        """是否存在未结清的借出单（借出中/逾期/借出后报失）。"""
        prefetched = self.__dict__.get("_prefetched_active_loans", None)
        if prefetched is not None:
            return bool(prefetched)
        return self.loans.filter(status__in=Loan.ACTIVE_STATUSES).exists()

    def active_loan(self):
        """返回未结清借出单；理论上至多一张。"""
        prefetched = self.__dict__.get("_prefetched_active_loans", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.loans.filter(status__in=Loan.ACTIVE_STATUSES).first()

    def current_location(self):
        """装订后返回装订册位置，否则返回自身位置。

        借出中时自身位置字段不动（仍是归还后要恢复的原架位）；
        当前保管位置（读者手中）由流通层 circulation_snapshot 表达。
        """
        entry = getattr(self, "binding_entry", None)
        if entry is not None:
            return entry.binding.location
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


# 未结清单据状态（类定义体的 Q() 需要模块级常量）
_ACTIVE_LOAN_STATUSES = ["checked_out", "lost"]


class Loan(models.Model):
    """本地流通单据（借出单）：一张单对应一个实体的一轮借还。

    合刊实物只是一个 Item，所以合刊从任一覆盖期号或条码查到的都是同一张单。
    单据状态由 LoanEvent 链物化而来；重启后可由 rebuild_loan_state() 重放恢复。
    """

    class LoanStatus(models.TextChoices):
        CHECKED_OUT = "checked_out", "借出中"
        RETURNED = "returned", "已归还"
        LOST = "lost", "遗失"

    ACTIVE_STATUSES = [LoanStatus.CHECKED_OUT, LoanStatus.LOST]

    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="loans",
    )
    status = models.CharField(
        "单据状态", max_length=12,
        choices=LoanStatus.choices, default=LoanStatus.CHECKED_OUT,
    )
    borrower = models.CharField("借阅人", max_length=100, blank=True)
    checkout_at = models.DateTimeField("借出时刻", null=True, blank=True)
    due_at = models.DateTimeField("到期时刻", null=True, blank=True)
    return_at = models.DateTimeField("归还时刻", null=True, blank=True)
    lost_at = models.DateTimeField("报失时刻", null=True, blank=True)
    # 借出时封存的架位：归还时必须恢复到同一位置
    shelf_location = models.CharField("借出时架位", max_length=100, blank=True)
    version = models.PositiveIntegerField("已应用事件版本", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["item", "status"]),
        ]
        constraints = [
            # 同一实物至多一张未结清单据：防止并发重复借出（PG 约束；
            # SQLite 测试库也自 3.8 起支持部分唯一索引）
            models.UniqueConstraint(
                fields=["item"],
                condition=Q(status__in=_ACTIVE_LOAN_STATUSES),
                name="uniq_active_loan_per_item",
            ),
        ]

    def __str__(self):
        return f"Loan#{self.id} {self.item.barcode} [{self.status}]"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_overdue(self):
        """逾期由到期时刻与当前时间派生，不作为事件存储。"""
        if self.status != self.LoanStatus.CHECKED_OUT or self.due_at is None:
            return False
        return timezone.now() > self.due_at

    def derived_status(self, now=None):
        """读时状态：checked_out / overdue / lost / returned。"""
        now = now or timezone.now()
        if self.status == self.LoanStatus.CHECKED_OUT and self.due_at and now > self.due_at:
            return "overdue"
        return self.status


class LoanEvent(models.Model):
    """流通事件（仅追加的审计链）。

    - event_id：幂等标识，全局唯一；重复提交同一 event_id 只回放、不再改状态。
    - seq：单内发生顺序（按到达系统的顺序分配，严格递增）。
    - occurred_at：业务发生时间（事件所述事实的时刻），可早于 seq 更大的事件——
      迟到事件若比已应用的最新处置更早，只入链（superseded），不覆盖当前状态。
    """

    class EventType(models.TextChoices):
        CHECKOUT = "checkout", "借出"
        RETURN = "return", "归还"
        LOST = "lost", "遗失"

    loan = models.ForeignKey(
        Loan, on_delete=models.PROTECT, related_name="events",
    )
    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="loan_events",
    )
    event_id = models.UUIDField("幂等标识", unique=True, db_index=True)
    seq = models.PositiveIntegerField("单内顺序")
    type = models.CharField("事件类型", max_length=10, choices=EventType.choices)
    occurred_at = models.DateTimeField("业务发生时间")
    recorded_at = models.DateTimeField("入库时间", auto_now_add=True)
    actor = models.CharField("操作员", max_length=100, blank=True)
    note = models.CharField("备注", max_length=255, blank=True)
    # 借出快照（其余类型留空），用于审计与事件重放
    borrower = models.CharField("借阅人快照", max_length=100, blank=True)
    due_at = models.DateTimeField("到期时刻快照", null=True, blank=True)
    shelf_location = models.CharField("架位快照", max_length=100, blank=True)
    # 该事件是否真正参与了状态物化（迟到/非法事件只留痕不生效）
    applied = models.BooleanField("是否已应用", default=True)
    superseded = models.BooleanField("迟到被覆盖", default=False)
    reject_reason = models.CharField("未生效原因", max_length=255, blank=True)

    class Meta:
        ordering = ["loan_id", "seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["loan", "seq"], name="uniq_event_seq_per_loan",
            ),
        ]

    def __str__(self):
        return f"{self.type}#{self.seq} loan={self.loan_id} applied={self.applied}"


# 用于 prefetch_related 当前借出单（定位接口、时间轴避免 N+1）
ACTIVE_LOAN_PREFETCH = Prefetch(
    "loans",
    queryset=Loan.objects.filter(status__in=Loan.ACTIVE_STATUSES),
    to_attr="_prefetched_active_loans",
)


def circulation_snapshot(item, now=None):
    """实体的实际可得性视图（定位接口与时间轴共用）。

    available    实际可借（在馆、未装订、未借出）
    bound        已装订（须以装订册整体流通）
    checked_out  借出中，custody 指向借阅人，带 due_at
    overdue      借出逾期（checked_out 的派生）
    lost         遗失（借出后报失或入藏后直接报失）
    """
    if item.is_bound:
        return {
            "availability": "bound",
            "active_loan_id": None,
            "borrower": None,
            "checkout_at": None,
            "due_at": None,
            "custody": f"装订册 {item.binding_entry.binding.call_number}",
            "custody_kind": "binding",
            "shelf_location": item.location,
        }
    loan = item.active_loan()
    if loan is not None:
        derived = loan.derived_status(now=now)
        return {
            "availability": derived,
            "active_loan_id": loan.id,
            "borrower": loan.borrower,
            "checkout_at": loan.checkout_at,
            "due_at": loan.due_at,
            "custody": f"借阅人：{loan.borrower}" if loan.borrower else "借出（在读者手中）",
            "custody_kind": "borrower",
            "shelf_location": item.location,
        }
    return {
        "availability": "lost" if item.status == Item.ItemStatus.LOST else "available",
        "active_loan_id": None,
        "borrower": None,
        "checkout_at": None,
        "due_at": None,
        "custody": item.current_location(),
        "custody_kind": "shelf",
        "shelf_location": item.location,
    }


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有在馆实物（含装订；借出中的实物仍属馆藏）
    issued+missing    已发行但缺藏（无实物或全部丢失）
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


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订、借出都可命中）。"""
    rows = []
    for issue in number.issues.all():
        for item in issue.items.select_related(
            "title", "binding_entry__binding",
        ).prefetch_related(ACTIVE_LOAN_PREFETCH):
            snap = circulation_snapshot(item)
            rows.append({
                "issue_id": issue.id,
                "barcode": item.barcode,
                "status": item.status,
                "location": item.current_location(),
                "bound": item.is_bound,
                "binding": item.binding_entry.binding.call_number if item.is_bound else None,
                # 实际可得性 / 当前保管位置 / 到期日
                "availability": snap["availability"],
                "custody": snap["custody"],
                "custody_kind": snap["custody_kind"],
                "active_loan_id": snap["active_loan_id"],
                "borrower": snap["borrower"],
                "due_at": snap["due_at"],
                "checkout_at": snap["checkout_at"],
            })
    return rows
