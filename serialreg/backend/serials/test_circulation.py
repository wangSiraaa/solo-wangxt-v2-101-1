"""流通单据与事件链验收：借出/归还/逾期/遗失、幂等、迟到事件、装订互斥、重放恢复。

运行：SERIALREG_DB=sqlite pytest -q
"""
import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from serials.circulation import rebuild_loan_state
from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, Item, Loan, LoanEvent, Title,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def combined(db):
    """《双月刊》：v.8 no.3-4 两期合刊（一个实物 CB-34，两个编号关系）+ no.5。"""
    t = Title.objects.create(title="合刊流通报", issn="4444-5555")
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED,
        issue_month="2024-03-01", issue_month_end="2024-04-01",
    )
    comb.numbers.set([n3, n4])
    item = Item.objects.create(
        barcode="CB-34", title=t, issue=comb, location="期刊区B-02",
    )
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    item5 = Item.objects.create(
        barcode="CB-05", title=t, issue=iss5, location="期刊区B-09",
    )
    return {"t": t, "n3": n3, "n4": n4, "n5": n5,
            "item": item, "item5": item5}


def _checkout(api, *, barcode="CB-34", due=None, borrower="张三",
              event_id=None, occurred_at=None, status_code=201):
    due = due or (timezone.now() + timedelta(days=14))
    payload = {
        "barcode": barcode, "borrower": borrower,
        "due_at": due.isoformat(),
        "event_id": event_id or str(uuid.uuid4()),
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at.isoformat()
    resp = api.post("/api/loans/checkout/", payload, format="json")
    assert resp.status_code == status_code, resp.json()
    return resp


# ---------- 验收 1：合刊从两个期号与条码得到同一借出单 ----------

@pytest.mark.django_db
def test_combined_checkout_same_loan_from_both_numbers_and_barcode(api, combined):
    t = combined["t"]
    due = timezone.now() + timedelta(days=10)
    resp = _checkout(api, due=due, borrower="李四")
    loan_id = resp.json()["id"]

    # 从 no.3、no.4 与条码三个入口都得到同一张借出单与同一到期日
    loans = []
    for num in ("3", "4"):
        r = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}")
        assert r.status_code == 200
        m = r.json()["matches"][0]
        assert m["barcode"] == "CB-34"
        assert m["availability"] == "checked_out"
        assert m["active_loan_id"] == loan_id
        assert m["borrower"] == "李四"
        assert m["custody_kind"] == "borrower"
        # 架位字段仍是原位置（归还要恢复），当前保管位置指向借阅人
        assert m["location"] == "期刊区B-02"
        assert "李四" in m["custody"]
        loans.append(m["active_loan_id"])
        assert m["due_at"] == due.isoformat().replace("+00:00", "Z") or \
            m["due_at"].startswith(due.isoformat()[:19])

    rb = api.get("/api/items/locate/?barcode=CB-34").json()["matches"][0]
    assert rb["active_loan_id"] == loan_id
    assert rb["availability"] == "checked_out"
    assert rb["due_at"].startswith(due.isoformat()[:19])
    assert loans == [loan_id, loan_id]

    # 时间轴上合刊两个槽位显示同一借出状态与到期日
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    loans_on_timeline = [
        it["active_loan_id"]
        for slot in tl["slots"] for iss in slot["issues"] for it in iss["items"]
        if it["barcode"] == "CB-34"
    ]
    assert loans_on_timeline == [loan_id, loan_id]

    # 实体与单据状态
    combined["item"].refresh_from_db()
    assert combined["item"].status == Item.ItemStatus.CHECKED_OUT
    assert Loan.objects.get(id=loan_id).shelf_location == "期刊区B-02"


@pytest.mark.django_db
def test_overdue_is_derived_and_shown(api, combined):
    past = timezone.now() - timedelta(days=1)
    _checkout(api, due=past)
    r = api.get("/api/items/locate/?barcode=CB-34").json()["matches"][0]
    assert r["availability"] == "overdue"
    loan = Loan.objects.get(item__barcode="CB-34")
    assert loan.is_overdue is True
    # 逾期不落事件：事件链仍只有一条 checkout
    assert list(loan.events.values_list("type", flat=True)) == ["checkout"]


# ---------- 验收 2：归还幂等 + 原位置恢复 ----------

@pytest.mark.django_db
def test_return_is_idempotent_and_restores_location(api, combined):
    item = combined["item"]
    # 借出（封存原架位）
    _checkout(api)
    item.refresh_from_db()
    loan = Loan.objects.get(item=item)
    assert item.status == Item.ItemStatus.CHECKED_OUT

    event_id = str(uuid.uuid4())
    payload = {"barcode": "CB-34", "event_id": event_id}
    r1 = api.post("/api/loans/return_item/", payload, format="json")
    assert r1.status_code == 201
    assert r1.json()["status"] == "returned"

    # 归还后原位置正确恢复
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
    assert item.location == "期刊区B-02"

    # 同一归还请求（相同 event_id）重放：只产生一次归还
    r2 = api.post("/api/loans/return_item/", payload, format="json")
    assert r2.status_code == 201
    assert r2.json()["event_result"]["replayed"] is True
    assert LoanEvent.objects.filter(
        loan=loan, type=LoanEvent.EventType.RETURN,
    ).count() == 1
    assert Loan.objects.filter(item=item).count() == 1
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
    assert item.location == "期刊区B-02"

    # 归还后从任一期号都显示在馆、架位恢复
    for num in ("3", "4"):
        m = api.get(
            f"/api/items/locate/?title={combined['t'].id}&volume=8&number={num}",
        ).json()["matches"][0]
        assert m["availability"] == "available"
        assert m["custody_kind"] == "shelf"
        assert m["location"] == "期刊区B-02"
        assert m["active_loan_id"] is None


@pytest.mark.django_db
def test_checkout_idempotent_replay(api, combined):
    """借出请求带同一 event_id 的网络重试也只产生一张单。"""
    event_id = str(uuid.uuid4())
    due = (timezone.now() + timedelta(days=7)).isoformat()
    payload = {"barcode": "CB-34", "due_at": due, "event_id": event_id,
               "borrower": "王五"}
    r1 = api.post("/api/loans/checkout/", payload, format="json")
    r2 = api.post("/api/loans/checkout/", payload, format="json")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["event_result"]["replayed"] is True
    assert Loan.objects.filter(item=combined["item"]).count() == 1
    assert LoanEvent.objects.filter(type=LoanEvent.EventType.CHECKOUT).count() == 1


# ---------- 验收 3：借出/装订互斥 ----------

@pytest.mark.django_db
def test_cannot_bind_checked_out_item(api, combined):
    t = combined["t"]
    _checkout(api, barcode="CB-34")
    # 借出中尝试装订 → 拒绝；装订关系不存在，合刊关系不变
    resp = api.post("/api/bindings/", {
        "call_number": "Q/LOCK", "title": t.id,
        "location": "装订库 X-1",
        "item_ids": [combined["item"].id, combined["item5"].id],
    }, format="json")
    assert resp.status_code == 400
    assert "借出" in json.dumps(resp.json(), ensure_ascii=False)
    assert Binding.objects.count() == 0
    assert not BindingEntry.objects.filter(item=combined["item"]).exists()
    # 借出单仍在
    loan = Loan.objects.get(item=combined["item"])
    assert loan.status == Loan.LoanStatus.CHECKED_OUT
    # 合刊编号关系完好
    assert set(
        combined["item"].issue.numbers.values_list("number", flat=True)
    ) == {"3", "4"}

    # 归还后可正常装订
    api.post("/api/loans/return_item/",
             {"barcode": "CB-34", "event_id": str(uuid.uuid4())},
             format="json")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/OK", "title": t.id,
        "location": "装订库 C-12",
        "item_ids": [combined["item"].id, combined["item5"].id],
    }, format="json")
    assert resp.status_code == 201, resp.json()


@pytest.mark.django_db
def test_cannot_checkout_bound_item(api, combined):
    t = combined["t"]
    resp = api.post("/api/bindings/", {
        "call_number": "Q/B", "title": t.id,
        "location": "装订库 C-12",
        "item_ids": [combined["item"].id, combined["item5"].id],
    }, format="json")
    assert resp.status_code == 201
    # 已装订实体尝试单件外借 → 拒绝
    r = api.post("/api/loans/checkout/", {
        "barcode": "CB-34",
        "due_at": (timezone.now() + timedelta(days=7)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert r.status_code == 409
    assert "装订" in r.json()["detail"]
    assert r.json()["code"] == "bound"
    assert Loan.objects.count() == 0
    assert LoanEvent.objects.count() == 0
    # 装订关系不变
    assert BindingEntry.objects.filter(item=combined["item"]).exists()


@pytest.mark.django_db
def test_cannot_checkout_twice(api, combined):
    _checkout(api)
    r = api.post("/api/loans/checkout/", {
        "barcode": "CB-34",
        "due_at": (timezone.now() + timedelta(days=7)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "already_on_loan"
    assert Loan.objects.filter(item=combined["item"]).count() == 1


@pytest.mark.django_db
def test_patch_status_blocked_while_checked_out(api, combined):
    _checkout(api)
    r = api.patch(f"/api/items/{combined['item'].id}/",
                  {"status": "lost"}, format="json")
    assert r.status_code == 400
    combined["item"].refresh_from_db()
    assert combined["item"].status == Item.ItemStatus.CHECKED_OUT


# ---------- 验收 4：逾期 + 迟到事件 + 重放/重启 ----------

@pytest.mark.django_db
def test_overdue_then_late_lost_keeps_newest_disposition(api, combined):
    """逾期后又收到较早的遗失事件：系统按事件顺序保留正确当前状态与完整历史。

    阶段一：逾期后收到一个「声称更早（借出前）」的遗失——迟到，只入链不生效；
    阶段二：当下正式报失——应用，当前状态为遗失；
    阶段三：再来一个较早的归还——迟到，不得覆盖较新的遗失处置。
    （另见 test_lost_then_late_return_does_not_restore：遗失后迟到归还同样被压下。）
    """
    now = timezone.now()
    # 30 天前借出，15 天前到期（现在逾期）
    _checkout(api, occurred_at=now - timedelta(days=30),
              due=now - timedelta(days=15))
    loan = Loan.objects.get(item=combined["item"])
    assert loan.is_overdue

    # 阶段一：迟到的遗失事件（声称 31 天前就丢了，早于已记录的借出处置）
    late = api.post("/api/loans/lost/", {
        "barcode": "CB-34",
        "occurred_at": (now - timedelta(days=31)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert late.status_code == 201
    body = late.json()
    assert body["event_result"]["superseded"] is True
    assert body["event_result"]["applied"] is False
    assert body["status"] == "checked_out"

    loan.refresh_from_db()
    combined["item"].refresh_from_db()
    # 当前状态仍是借出（读时派生为逾期），未被更早的遗失覆盖
    assert loan.status == Loan.LoanStatus.CHECKED_OUT
    assert loan.is_overdue
    assert combined["item"].status == Item.ItemStatus.CHECKED_OUT
    # 定位接口仍是逾期借出
    m = api.get("/api/items/locate/?barcode=CB-34").json()["matches"][0]
    assert m["availability"] == "overdue"
    assert m["active_loan_id"] == loan.id

    # 阶段二：当下正式报失 → 应用
    fresh = api.post("/api/loans/lost/", {
        "barcode": "CB-34",
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert fresh.status_code == 201
    assert fresh.json()["status"] == "lost"
    loan.refresh_from_db()
    combined["item"].refresh_from_db()
    assert loan.status == Loan.LoanStatus.LOST
    assert combined["item"].status == Item.ItemStatus.LOST

    # 阶段三：更早的归还（迟到）不能翻转遗失结论
    late_return = api.post("/api/loans/return_item/", {
        "barcode": "CB-34",
        "occurred_at": (now - timedelta(days=10)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert late_return.json()["event_result"]["superseded"] is True
    loan.refresh_from_db()
    assert loan.status == Loan.LoanStatus.LOST

    # 完整历史：checkout + 迟到 lost + 正式 lost + 迟到 return，顺序与生效标记齐全
    events = list(loan.events.order_by("seq"))
    assert [e.type for e in events] == ["checkout", "lost", "lost", "return"]
    assert [e.applied for e in events] == [True, False, True, False]
    assert [e.superseded for e in events] == [False, True, False, True]
    assert all(e.reject_reason for e in events if not e.applied)


@pytest.mark.django_db
def test_lost_then_late_return_does_not_restore(api, combined):
    """较新处置是遗失：迟到的归还事件不得把它改回在馆。"""
    now = timezone.now()
    _checkout(api, occurred_at=now - timedelta(days=10),
              due=now - timedelta(days=5))
    # 当天报失（较新处置）
    api.post("/api/loans/lost/", {
        "barcode": "CB-34", "event_id": str(uuid.uuid4()),
        "occurred_at": (now - timedelta(days=2)).isoformat(),
    }, format="json")
    # 迟到归还：声称 6 天前还的（早于报失）
    r = api.post("/api/loans/return_item/", {
        "barcode": "CB-34",
        "occurred_at": (now - timedelta(days=6)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert r.status_code == 201
    er = r.json()["event_result"]
    assert er["superseded"] is True and er["applied"] is False
    loan = Loan.objects.get(item=combined["item"])
    assert loan.status == Loan.LoanStatus.LOST
    combined["item"].refresh_from_db()
    assert combined["item"].status == Item.ItemStatus.LOST
    assert combined["item"].location == "期刊区B-02"  # 架位从未被覆盖


@pytest.mark.django_db
def test_rebuild_after_restart_preserves_state_and_history(api, combined):
    """模拟刷新/重启：把物化状态改乱，再按事件链重放恢复。"""
    now = timezone.now()
    _checkout(api, occurred_at=now - timedelta(days=30),
              due=now - timedelta(days=15), borrower="赵六")
    loan = Loan.objects.get(item=combined["item"])
    # 迟到遗失（不生效）+ 正常报失（生效）
    api.post("/api/loans/lost/", {
        "barcode": "CB-34",
        "occurred_at": (now - timedelta(days=31)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    api.post("/api/loans/lost/", {
        "barcode": "CB-34", "event_id": str(uuid.uuid4()),
    }, format="json")

    # 重启前状态被外部改乱
    Loan.objects.filter(pk=loan.id).update(status="checked_out", version=0)
    Item.objects.filter(pk=combined["item"].id).update(status="available")

    # 重放恢复
    rebuild_loan_state(loan=Loan.objects.get(pk=loan.id))
    loan.refresh_from_db()
    combined["item"].refresh_from_db()
    assert loan.status == Loan.LoanStatus.LOST
    assert combined["item"].status == Item.ItemStatus.LOST
    # 只计应用事件：checkout + 新鲜 lost = 2
    assert loan.version == 2
    # 历史完整保留 3 条
    assert loan.events.count() == 3

    # 管理命令同样可用
    import io
    from django.core.management import call_command
    out = io.StringIO()
    call_command("rebuild_loans", "--loan-id", str(loan.id), stdout=out)
    assert "重建 1 张" in out.getvalue()


@pytest.mark.django_db
def test_rebuild_returned_loan_restores_shelf(api, combined):
    _checkout(api)
    loan = Loan.objects.get(item=combined["item"])
    api.post("/api/loans/return_item/",
             {"barcode": "CB-34", "event_id": str(uuid.uuid4())},
             format="json")
    # 改乱后重放：在馆 + 原架位恢复
    Item.objects.filter(pk=combined["item"].id).update(
        status="lost", location="错误位置")
    rebuild_loan_state(loan=Loan.objects.get(pk=loan.id))
    combined["item"].refresh_from_db()
    assert combined["item"].status == Item.ItemStatus.AVAILABLE
    assert combined["item"].location == "期刊区B-02"


# ---------- 事件链读取与非法转换 ----------

@pytest.mark.django_db
def test_events_endpoint_full_history(api, combined):
    _checkout(api)
    loan = Loan.objects.get(item=combined["item"])
    r = api.get(f"/api/loans/{loan.id}/events/")
    assert r.status_code == 200
    data = r.json()
    assert data["loan"] == loan.id
    assert [e["type"] for e in data["events"]] == ["checkout"]
    assert data["events"][0]["event_id"]

    # 归还后再报遗失：非法转换，事件只留痕
    api.post("/api/loans/return_item/",
             {"barcode": "CB-34", "event_id": str(uuid.uuid4())},
             format="json")
    r = api.post("/api/loans/lost/",
                 {"barcode": "CB-34", "event_id": str(uuid.uuid4())},
                 format="json")
    assert r.status_code == 201
    assert r.json()["event_result"]["applied"] is False
    assert LoanEvent.objects.filter(loan=loan, type="lost").count() == 1
    assert Loan.objects.get(pk=loan.id).status == Loan.LoanStatus.RETURNED


@pytest.mark.django_db
def test_return_without_loan_rejected(api, combined):
    r = api.post("/api/loans/return_item/",
                 {"barcode": "CB-34", "event_id": str(uuid.uuid4())},
                 format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "no_loan"


@pytest.mark.django_db
def test_lost_without_loan_rejected(api, combined):
    r = api.post("/api/loans/lost/",
                 {"barcode": "CB-34", "event_id": str(uuid.uuid4())},
                 format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "no_loan"
    assert LoanEvent.objects.count() == 0


@pytest.mark.django_db
def test_checkout_lost_item_rejected(api, combined):
    # 未入流通直接报失（原有缺藏通道）
    item = combined["item"]
    item.status = Item.ItemStatus.LOST
    item.save()
    r = api.post("/api/loans/checkout/", {
        "barcode": "CB-34",
        "due_at": (timezone.now() + timedelta(days=7)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "lost"


@pytest.mark.django_db
def test_same_loan_via_loan_and_barcode_idempotent(api, combined):
    """从 loan 主键与条码两个入口提交同一 event_id，仍是同一条事件。"""
    r = _checkout(api)
    loan_id = r.json()["id"]
    event_id = str(uuid.uuid4())
    a = api.post("/api/loans/return_item/",
                 {"loan": loan_id, "event_id": event_id}, format="json")
    b = api.post("/api/loans/return_item/",
                 {"barcode": "CB-34", "event_id": event_id}, format="json")
    assert a.status_code == 201 and b.status_code == 201
    assert b.json()["event_result"]["replayed"] is True
    assert LoanEvent.objects.filter(type="return").count() == 1


@pytest.mark.django_db
def test_in_order_late_return_before_checkout_recorded(api, combined):
    """归还事件的发生时间早于借出：迟到留痕，单据仍是借出中。"""
    now = timezone.now()
    _checkout(api, occurred_at=now - timedelta(days=5))
    r = api.post("/api/loans/return_item/", {
        "barcode": "CB-34",
        "occurred_at": (now - timedelta(days=10)).isoformat(),
        "event_id": str(uuid.uuid4()),
    }, format="json")
    er = r.json()["event_result"]
    assert er["applied"] is False and er["superseded"] is True
    loan = Loan.objects.get(item=combined["item"])
    assert loan.status == Loan.LoanStatus.CHECKED_OUT


@pytest.mark.django_db
def test_loan_list_filters(api, combined):
    _checkout(api)
    rows = api.get("/api/loans/?barcode=CB-34").json()
    assert len(rows) >= 1
    active = api.get("/api/loans/?active=1").json()
    assert all(loan["status"] in ("checked_out", "lost") for loan in active)
