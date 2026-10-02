"""本地流通领域服务：基于事件链的借出 / 归还 / 遗失。

不变量：
  1. LoanEvent 仅追加，每条带全局幂等 event_id 与单内递增 seq；
  2. 重复提交（同 event_id）只回放，不重复改变状态；
  3. 迟到事件（occurred_at 早于已应用的最新处置）只入审计链，不覆盖当前状态；
  4. 借出中的实体不能再次借出，也不能装订；已装订实体不能单件外借；
  5. 归还必须把实体恢复到借出时封存的同一架位。
"""
import uuid

from django.db import transaction
from django.utils import timezone

from .models import Item, Loan, LoanEvent


class CirculationError(Exception):
    """流通业务规则被违反（映射为 HTTP 409/400）。"""

    def __init__(self, detail, code="invalid"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _coerce_uuid(event_id):
    if not event_id:
        return uuid.uuid4()
    try:
        return uuid.UUID(str(event_id))
    except (ValueError, AttributeError, TypeError):
        raise CirculationError("event_id 必须是 UUID。", code="bad_request")


def _get_item(item_id=None, barcode=None):
    qs = Item.objects.select_for_update().select_related(
        "binding_entry__binding",
    )
    if barcode:
        item = qs.filter(barcode=barcode).first()
    elif item_id:
        item = qs.filter(pk=item_id).first()
    else:
        raise CirculationError("需要提供 item 或 barcode。", code="bad_request")
    if item is None:
        raise CirculationError("找不到该实体。", code="not_found")
    return item


@transaction.atomic
def checkout(*, item_id=None, barcode=None, borrower="", due_at,
             occurred_at=None, event_id=None, actor="", note=""):
    """借出：只有「未装订且在馆」的实体可借出。"""
    event_uuid = _coerce_uuid(event_id)
    # 幂等：event_id 已存在（可能来自网络重试），直接回放原结果
    existing = LoanEvent.objects.filter(event_id=event_uuid).first()
    if existing is not None:
        return _result(existing.loan, existing, replayed=True)

    item = _get_item(item_id, barcode)
    if item.is_bound:
        raise CirculationError(
            "已装订实体不能拆成单件外借，请以装订册整体处理。", code="bound",
        )
    active = item.loans.filter(status__in=Loan.ACTIVE_STATUSES).first()
    if active is not None:
        raise CirculationError(
            f"实体 {item.barcode} 已有未结清借出单 #{active.id}，不能重复借出。",
            code="already_on_loan",
        )
    if item.status == Item.ItemStatus.LOST:
        raise CirculationError(
            f"实体 {item.barcode} 已标记遗失，不能借出。", code="lost",
        )
    # 防御性：任何非 available 的异常状态都不允许借出
    if item.status != Item.ItemStatus.AVAILABLE:
        raise CirculationError(
            f"实体当前状态为 {item.status}，不能借出。", code="not_available",
        )
    if due_at is None:
        raise CirculationError("必须提供到期时刻 due_at。", code="bad_request")
    occurred_at = occurred_at or timezone.now()

    loan = Loan.objects.create(
        item=item,
        status=Loan.LoanStatus.CHECKED_OUT,
        borrower=borrower or "",
        checkout_at=occurred_at,
        due_at=due_at,
        shelf_location=item.location or "",
        version=1,
    )
    event = LoanEvent.objects.create(
        loan=loan, item=item, event_id=event_uuid, seq=1,
        type=LoanEvent.EventType.CHECKOUT, occurred_at=occurred_at,
        actor=actor or "", note=note or "",
        borrower=borrower or "", due_at=due_at,
        shelf_location=item.location or "", applied=True,
    )
    item.status = Item.ItemStatus.CHECKED_OUT
    item.save(update_fields=["status"])
    return _result(loan, event, replayed=False)


@transaction.atomic
def apply_event(event_type, *, loan_id=None, item_id=None, barcode=None,
                occurred_at=None, event_id=None, actor="", note=""):
    """向借出单追加归还/遗失事件（幂等、保序）。

    返回 (loan, event, meta)；meta 含 replayed / superseded / reason。
    """
    event_uuid = _coerce_uuid(event_id)
    existing = LoanEvent.objects.select_related("loan").filter(
        event_id=event_uuid,
    ).first()
    if existing is not None:
        return _result(existing.loan, existing, replayed=True)

    occurred_at = occurred_at or timezone.now()

    # 先（无锁）确定实体，再统一按 item → loan 顺序加锁，避免与 checkout 死锁
    item = _get_item(item_id, barcode) if (item_id or barcode) else None

    loan = None
    if loan_id:
        peek = Loan.objects.filter(pk=loan_id).first()
        if peek is None:
            raise CirculationError("找不到该借出单。", code="not_found")
        if item is not None and item.id != peek.item_id:
            raise CirculationError("借出单与实体不匹配。", code="bad_request")
        if item is None:
            item = _get_item(item_id=peek.item_id)

    # item 行已持有行锁
    if loan_id:
        loan = Loan.objects.select_for_update().get(pk=loan_id)
    else:
        loan = item.loans.order_by("-id").first()
    if loan is None:
        raise CirculationError("该实体没有借出单，无法登记归还/遗失。",
                               code="no_loan")

    seq = (
        LoanEvent.objects.filter(loan=loan)
        .select_for_update()
        .count()
    ) + 1

    applied_events = list(
        LoanEvent.objects.filter(loan=loan, applied=True).order_by("-seq")
    )
    latest = applied_events[0] if applied_events else None

    event = LoanEvent(
        loan=loan, item=item, event_id=event_uuid, seq=seq,
        type=event_type, occurred_at=occurred_at,
        actor=actor or "", note=note or "",
        shelf_location=loan.shelf_location or "",
    )

    # 迟到事件：业务发生时间早于已应用的最新处置 → 只留痕，不覆盖较新处置
    if latest is not None and occurred_at < latest.occurred_at:
        event.applied = False
        event.superseded = True
        event.reject_reason = (
            f"迟到事件：发生时间 {occurred_at:%Y-%m-%d %H:%M} 早于已应用的"
            f"{latest.get_type_display()}（{latest.occurred_at:%Y-%m-%d %H:%M}），"
            "保留较新处置，当前状态不变。"
        )
        event.save()
        return _result(loan, event, replayed=False)

    # 到达顺序下的状态机
    reason = _transition(loan, item, event_type, occurred_at)
    if reason:
        event.applied = False
        event.reject_reason = reason
        event.save()
        return _result(loan, event, replayed=False, rejected=True)

    event.applied = True
    event.save()
    loan.version += 1
    loan.save()
    return _result(loan, event, replayed=False)


def _transition(loan, item, event_type, occurred_at):
    """在到达顺序上应用一次处置；返回非空字符串表示非法（不应用）。"""
    if loan.status == Loan.LoanStatus.CHECKED_OUT:
        if event_type == LoanEvent.EventType.RETURN:
            loan.status = Loan.LoanStatus.RETURNED
            loan.return_at = occurred_at
            loan.save(update_fields=["status", "return_at", "version"])
            item.status = Item.ItemStatus.AVAILABLE
            # 原位置正确恢复：借出时封存的架位
            item.location = loan.shelf_location or ""
            item.save(update_fields=["status", "location"])
            return ""
        if event_type == LoanEvent.EventType.LOST:
            loan.status = Loan.LoanStatus.LOST
            loan.lost_at = occurred_at
            loan.save(update_fields=["status", "lost_at", "version"])
            item.status = Item.ItemStatus.LOST
            item.save(update_fields=["status"])
            return ""
        return f"借出中的单据不接受 {event_type} 事件。"

    if loan.status == Loan.LoanStatus.LOST:
        return f"借出单已按遗失结清（{loan.lost_at:%Y-%m-%d %H:%M}），后续处置不再改变状态。"
    if loan.status == Loan.LoanStatus.RETURNED:
        return f"借出单已于 {loan.return_at:%Y-%m-%d %H:%M} 归还结清，不能重复处置。"
    return f"单据状态 {loan.status} 不接受该事件。"


def return_item(**kwargs):
    return apply_event(LoanEvent.EventType.RETURN, **kwargs)


def report_lost(**kwargs):
    return apply_event(LoanEvent.EventType.LOST, **kwargs)


def _result(loan, event, replayed=False, rejected=False):
    return {
        "loan": loan,
        "event": event,
        # 重复提交只回放：不重复改变状态
        "replayed": replayed,
        "rejected": rejected and not replayed,
        "superseded": bool(event.superseded) and not replayed,
        "applied": bool(event.applied),
    }


# ---------- 事件重放（重启恢复 / 状态修复） ----------

@transaction.atomic
def rebuild_loan_state(loan=None, item=None):
    """从事件链重放物化 Loan/Item 状态。

    逾期是派生状态，无需重放；完整历史始终保留在 LoanEvent 表中。
    可对单张单据或某个实体的全部单据执行。
    """
    if loan is not None:
        loans = [loan]
    elif item is not None:
        loans = list(item.loans.all())
    else:
        loans = list(Loan.objects.all())
    return [_replay_one(ln) for ln in loans]


def _replay_one(loan):
    events = list(loan.events.order_by("seq"))
    item = loan.item

    loan.status = None
    loan.borrower = ""
    loan.checkout_at = None
    loan.due_at = None
    loan.return_at = None
    loan.lost_at = None
    loan.shelf_location = ""
    version = 0

    for ev in events:
        if not ev.applied:
            continue
        version += 1
        if ev.type == LoanEvent.EventType.CHECKOUT:
            loan.status = Loan.LoanStatus.CHECKED_OUT
            loan.borrower = ev.borrower
            loan.checkout_at = ev.occurred_at
            loan.due_at = ev.due_at
            loan.shelf_location = ev.shelf_location
        elif ev.type == LoanEvent.EventType.RETURN:
            loan.status = Loan.LoanStatus.RETURNED
            loan.return_at = ev.occurred_at
        elif ev.type == LoanEvent.EventType.LOST:
            loan.status = Loan.LoanStatus.LOST
            loan.lost_at = ev.occurred_at
    loan.version = version
    loan.save()

    # 实体状态取其未结清单据（同时至多一张）；无未结清则看最近一张是否已归还
    active = (
        item.loans.filter(status__in=Loan.ACTIVE_STATUSES)
        .order_by("-id").first()
    )
    if active is not None:
        item.status = (
            Item.ItemStatus.LOST
            if active.status == Loan.LoanStatus.LOST
            else Item.ItemStatus.CHECKED_OUT
        )
        item.save(update_fields=["status"])
    else:
        last_loan = item.loans.order_by("-id").first()
        if last_loan is not None and last_loan.status == Loan.LoanStatus.RETURNED:
            # 归还态：恢复在馆并回到借出时封存的同一架位
            item.status = Item.ItemStatus.AVAILABLE
            item.location = last_loan.shelf_location or ""
            item.save(update_fields=["status", "location"])
    return loan
