"""本地流通状态机：借出、归还、逾期、遗失。

设计要点：
  - 每次提交都带幂等标识；重放命中已记录的单据/事件，直接返回，不重复改变状态；
  - 事件按 occurred_at（业务发生时间）排序应用：早于单据最近已应用事件的
    迟到事件只写入审计链（applied=False），不覆盖较新的处置；
  - 借出中的实体不能被装订或再次借出；已装订实体不能拆成单件外借；
  - 借出不改动 Item.location（馆藏位置），归还后定位自然回到原位置。
"""
from django.db import transaction
from django.utils import timezone

from .models import CirculationEvent, Item, Loan


class CirculationError(Exception):
    """流通业务校验失败。code 供视图映射 HTTP 状态码。"""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


def _aware(dt):
    if dt is not None and timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


@transaction.atomic
def checkout_item(*, item, idempotency_key, due_date,
                  custody_location="", occurred_at=None):
    """开一张流通单（借出）。返回 (loan, replayed)。

    幂等：同一 idempotency_key 重放 → 返回原单据，不产生第二张单。
    """
    existing = Loan.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, True

    occurred_at = _aware(occurred_at) or timezone.now()
    item = Item.objects.select_for_update().get(pk=item.pk)
    if item.is_bound or item.status == Item.ItemStatus.BOUND:
        raise CirculationError(
            f"实物 {item.barcode} 已装订，不能拆成单件外借，请先拆订。",
        )
    if item.status == Item.ItemStatus.CHECKED_OUT:
        raise CirculationError(
            f"实物 {item.barcode} 已在借出中，不能重复借出。", code="conflict",
        )
    if item.status == Item.ItemStatus.LOST:
        raise CirculationError(f"实物 {item.barcode} 已遗失，不能借出。")

    loan = Loan.objects.create(
        item=item,
        idempotency_key=idempotency_key,
        due_date=due_date,
        custody_location=custody_location or "流通台",
        checked_out_at=occurred_at,
        last_seq=1,
        last_applied_occurred_at=occurred_at,
    )
    CirculationEvent.objects.create(
        loan=loan,
        event_type=CirculationEvent.EventType.CHECKOUT,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        seq=1,
        note="借出开单",
    )
    item.status = Item.ItemStatus.CHECKED_OUT
    item.save(update_fields=["status"])
    return loan, False


@transaction.atomic
def apply_event(*, loan_id, event_type, idempotency_key,
                occurred_at=None, note=""):
    """向流通单追加一个事件。返回 (event, replayed)。

    - 幂等：同一 idempotency_key 重放 → 返回已记录事件，状态不变；
    - 迟到（occurred_at 早于最近已应用事件）→ 记录 applied=False，状态不变；
    - 单据已关闭后到达的「新」事件（非迟到）→ 409 冲突，不入链。
    """
    valid = (
        CirculationEvent.EventType.RETURN,
        CirculationEvent.EventType.OVERDUE,
        CirculationEvent.EventType.LOST,
    )
    if event_type not in valid:
        # checkout 事件只能由开单流程产生
        raise CirculationError(f"不支持的事件类型：{event_type}")
    existing = CirculationEvent.objects.filter(
        idempotency_key=idempotency_key,
    ).select_related("loan").first()
    if existing is not None:
        return existing, True

    occurred_at = _aware(occurred_at) or timezone.now()
    loan = Loan.objects.select_for_update().get(pk=loan_id)
    seq = loan.last_seq + 1

    if occurred_at < loan.last_applied_occurred_at:
        # 迟到事件：写入审计链但不得覆盖较新的处置
        event = CirculationEvent.objects.create(
            loan=loan, event_type=event_type,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at, seq=seq, applied=False,
            note=note or "迟到事件：早于当前状态对应的事件时间，未覆盖较新处置。",
        )
        loan.last_seq = seq
        loan.save(update_fields=["last_seq"])
        return event, False

    if loan.status == Loan.Status.CLOSED:
        raise CirculationError(
            f"流通单 #{loan.pk} 已关闭（{loan.get_close_reason_display()}），"
            "事件未被接受。", code="conflict",
        )

    item = Item.objects.select_for_update().get(pk=loan.item_id)
    if event_type == CirculationEvent.EventType.RETURN:
        loan.status = Loan.Status.CLOSED
        loan.close_reason = Loan.CloseReason.RETURNED
        loan.closed_at = occurred_at
        # Item.location 在借出期间从未被改动，归还后定位自然回到原位置
        item.status = Item.ItemStatus.AVAILABLE
    elif event_type == CirculationEvent.EventType.OVERDUE:
        loan.is_overdue = True
    elif event_type == CirculationEvent.EventType.LOST:
        loan.status = Loan.Status.CLOSED
        loan.close_reason = Loan.CloseReason.LOST
        loan.closed_at = occurred_at
        item.status = Item.ItemStatus.LOST

    event = CirculationEvent.objects.create(
        loan=loan, event_type=event_type,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at, seq=seq, applied=True, note=note,
    )
    loan.last_seq = seq
    loan.last_applied_occurred_at = occurred_at
    loan.save()
    item.save(update_fields=["status"])
    return event, False
