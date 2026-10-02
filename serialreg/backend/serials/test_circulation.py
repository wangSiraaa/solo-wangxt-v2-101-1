"""本地流通验收：合刊同一单据、幂等重放、装订互斥、迟到事件与重启恢复。

运行：SERIALREG_DB=sqlite pytest -q
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, CirculationEvent, Issue, IssueNumber, Item, Loan,
    Title,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def combined_title(db):
    """《合刊报》：v.8 no.3-4 两期合刊（实物 CB-34），no.5 普通期（CB-05）。"""
    t = Title.objects.create(title="合刊报", issn="3333-4444")
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED,
        issue_month="2024-03-01", issue_month_end="2024-04-01",
    )
    comb.numbers.set([n3, n4])
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    item = Item.objects.create(
        barcode="CB-34", title=t, issue=comb, location="期刊区B-02",
    )
    item5 = Item.objects.create(
        barcode="CB-05", title=t, issue=iss5, location="期刊区B-09",
    )
    return t, {"n3": n3, "n4": n4, "n5": n5}, item, item5


def checkout(api, item, key, **kw):
    payload = {
        "item": item.id, "idempotency_key": key,
        "due_date": kw.pop("due_date", "2026-10-30"),
    }
    payload.update(kw)
    return api.post("/api/loans/", payload, format="json")


def post_event(api, loan_id, event_type, key, occurred_at=None):
    payload = {"event_type": event_type, "idempotency_key": key}
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at.isoformat()
    return api.post(f"/api/loans/{loan_id}/events/", payload, format="json")


# ---------- 验收 1：合刊借出后，两个期号与条码都指向同一借出单 ----------

@pytest.mark.django_db
def test_combined_checkout_same_loan_from_both_numbers_and_barcode(
    combined_title, api,
):
    t, nums, item, _ = combined_title
    resp = checkout(api, item, "ck-1", custody_location="流通台-03")
    assert resp.status_code == 201, resp.json()
    loan_id = resp.json()["id"]

    seen = []
    for num in ("3", "4"):
        r = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}")
        assert r.status_code == 200
        m = r.json()["matches"][0]
        assert m["barcode"] == "CB-34"
        assert m["availability"] == "on_loan"
        assert m["location"] == "流通台-03"  # 当前保管位置
        seen.append(m["circulation"])
    r = api.get("/api/items/locate/?barcode=CB-34")
    m = r.json()["matches"][0]
    assert m["availability"] == "on_loan"
    seen.append(m["circulation"])

    # 三个入口看到的是同一张借出单、同一个到期日
    assert {c["loan_id"] for c in seen} == {loan_id}
    assert {c["due_date"] for c in seen} == {"2026-10-30"}
    assert all(c["custody_location"] == "流通台-03" for c in seen)

    # 时间轴上两个期号槽位同样显示借出中与到期日
    tl = api.get(f"/api/timeline/?title={t.id}").json()
    for slot in tl["slots"]:
        if slot["number"] in ("3", "4"):
            circs = [
                it["circulation"] for iss in slot["issues"]
                for it in iss["items"]
            ]
            assert {c["loan_id"] for c in circs} == {loan_id}
            assert {c["due_date"] for c in circs} == {"2026-10-30"}
            assert all(
                it["availability"] == "on_loan"
                for iss in slot["issues"] for it in iss["items"]
            )


# ---------- 验收 2：归还重放只产生一次归还，原位置恢复 ----------

@pytest.mark.django_db
def test_return_replay_is_idempotent_and_location_restored(
    combined_title, api,
):
    t, nums, item, _ = combined_title
    loan = checkout(api, item, "ck-2").json()

    # 同一归还请求（同一幂等标识）重放三次
    first = post_event(api, loan["id"], "return", "ret-1")
    assert first.status_code == 201, first.json()
    for _ in range(2):
        replay = post_event(api, loan["id"], "return", "ret-1")
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

    # 事件链里只有一个归还事件，单据只关闭一次
    events = api.get(f"/api/loans/{loan['id']}/events/").json()
    returns = [e for e in events if e["event_type"] == "return"]
    assert len(returns) == 1
    assert CirculationEvent.objects.filter(
        loan_id=loan["id"], event_type="return",
    ).count() == 1

    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
    # 借出期间未改动馆藏位置字段，归还后定位回到原位置
    assert item.location == "期刊区B-02"
    assert item.current_location() == "期刊区B-02"
    r = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=4")
    m = r.json()["matches"][0]
    assert m["location"] == "期刊区B-02"
    assert m["availability"] == "available"
    assert m["circulation"] is None

    # 开单本身也幂等：同一 key 重放不产生第二张单
    again = checkout(api, item, "ck-2")
    assert again.status_code == 200
    assert again.json()["id"] == loan["id"]
    assert Loan.objects.filter(item=item).count() == 1


# ---------- 验收 3：借出↔装订互斥，关系不被破坏 ----------

@pytest.mark.django_db
def test_checked_out_item_cannot_be_bound(combined_title, api):
    t, nums, item, item5 = combined_title
    checkout(api, item, "ck-3")

    resp = api.post("/api/bindings/", {
        "call_number": "Q/NO", "title": t.id, "location": "装订库 X",
        "item_ids": [item.id, item5.id],
    }, format="json")
    assert resp.status_code == 400
    assert "借出中" in str(resp.json())
    # 原关系不变：没有装订册，实物仍是借出状态
    assert Binding.objects.count() == 0
    assert not BindingEntry.objects.filter(item=item).exists()
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.CHECKED_OUT
    assert item.current_location() == "流通台"


@pytest.mark.django_db
def test_bound_item_cannot_be_checked_out_individually(combined_title, api):
    t, nums, item, item5 = combined_title
    binding = Binding.objects.create(
        call_number="Q/HK-1", title=t, location="装订库 C-12",
    )
    BindingEntry.objects.create(
        item=item, binding=binding, previous_location=item.location,
    )
    Item.objects.filter(id=item.id).update(status=Item.ItemStatus.BOUND)

    resp = checkout(api, item, "ck-4")
    assert resp.status_code == 400
    assert "装订" in str(resp.json())
    # 装订关系不变，没有产生流通单
    assert Loan.objects.count() == 0
    item.refresh_from_db()
    assert item.is_bound
    assert item.status == Item.ItemStatus.BOUND
    assert item.current_location() == "装订库 C-12"


@pytest.mark.django_db
def test_checked_out_item_cannot_be_checked_out_again(combined_title, api):
    t, nums, item, _ = combined_title
    assert checkout(api, item, "ck-5").status_code == 201
    # 换一个幂等标识再借 → 冲突；同一标识 → 重放原单
    assert checkout(api, item, "ck-5b").status_code == 409
    replay = checkout(api, item, "ck-5")
    assert replay.status_code == 200
    assert Loan.objects.filter(item=item).count() == 1


# ---------- 验收 4：逾期后迟到事件不覆盖，重启后状态与历史完整 ----------

@pytest.mark.django_db
def test_overdue_then_stale_lost_event_does_not_override(combined_title, api):
    t, nums, item, _ = combined_title
    base = timezone.now() - timedelta(days=10)
    loan = checkout(
        api, item, "ck-6", occurred_at=base.isoformat(), due_date="2026-09-20",
    ).json()

    # T+5d 标记逾期
    od = post_event(api, loan["id"], "overdue", "od-1",
                    occurred_at=base + timedelta(days=5))
    assert od.status_code == 201
    assert od.json()["loan"]["is_overdue"] is True

    # 迟到：声称 T+2d 发生的遗失事件 → 入审计链但不覆盖逾期后的在借状态
    stale = post_event(api, loan["id"], "lost", "lost-stale",
                       occurred_at=base + timedelta(days=2))
    assert stale.status_code == 201
    body = stale.json()
    assert body["event"]["applied"] is False
    assert body["loan"]["status"] == "open"

    item.refresh_from_db()
    assert item.status == Item.ItemStatus.CHECKED_OUT  # 未被判遗失

    # 模拟刷新/重启：全新客户端从持久化读回，状态与完整历史一致
    fresh = APIClient()
    detail = fresh.get(f"/api/loans/{loan['id']}/").json()
    assert detail["status"] == "open"
    assert detail["is_overdue"] is True
    chain = [(e["seq"], e["event_type"], e["applied"]) for e in detail["events"]]
    assert chain == [
        (1, "checkout", True),
        (2, "overdue", True),
        (3, "lost", False),  # 迟到事件留痕但未应用
    ]
    r = fresh.get(f"/api/items/locate/?title={t.id}&volume=8&number=3")
    m = r.json()["matches"][0]
    assert m["availability"] == "on_loan"
    assert m["circulation"]["overdue"] is True

    # 较新的遗失事件（T+6d）正常应用：单据关闭、实物遗失
    lost = post_event(api, loan["id"], "lost", "lost-ok",
                      occurred_at=base + timedelta(days=6))
    assert lost.status_code == 201
    assert lost.json()["event"]["applied"] is True
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.LOST
    detail = fresh.get(f"/api/loans/{loan['id']}/").json()
    assert detail["status"] == "closed"
    assert detail["close_reason"] == "lost"
    assert len(detail["events"]) == 4

    # 已关闭单据上的新事件（非迟到）→ 409，不入链
    late = post_event(api, loan["id"], "return", "ret-late",
                      occurred_at=base + timedelta(days=7))
    assert late.status_code == 409
    assert CirculationEvent.objects.filter(loan_id=loan["id"]).count() == 4


@pytest.mark.django_db
def test_stale_return_does_not_override_newer_disposition(combined_title, api):
    """归还后又收到更早的遗失事件：实物保持已归还在馆。"""
    t, nums, item, _ = combined_title
    base = timezone.now() - timedelta(days=3)
    loan = checkout(api, item, "ck-7", occurred_at=base.isoformat()).json()
    post_event(api, loan["id"], "return", "ret-7",
               occurred_at=base + timedelta(days=1))
    stale = post_event(api, loan["id"], "lost", "lost-stale-7",
                       occurred_at=base + timedelta(hours=12))
    assert stale.json()["event"]["applied"] is False
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
    assert item.current_location() == "期刊区B-02"


# ---------- 事件链完整性：借出中的实物不能被直接改状态 ----------

@pytest.mark.django_db
def test_item_status_patch_blocked_while_on_loan(combined_title, api):
    t, nums, item, _ = combined_title
    loan = checkout(api, item, "ck-8").json()
    resp = api.patch(f"/api/items/{item.id}/", {"status": "lost"}, format="json")
    assert resp.status_code == 400
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.CHECKED_OUT
    # 归还后可以正常报失
    post_event(api, loan["id"], "return", "ret-8")
    resp = api.patch(f"/api/items/{item.id}/", {"status": "lost"}, format="json")
    assert resp.status_code == 200
