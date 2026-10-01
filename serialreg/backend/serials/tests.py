"""需求验证：跨年卷、停刊月份、两期合刊、装订与拆订、缺号不自动等同缺藏。

运行：SERIALREG_DB=sqlite pytest -q（有 PG 时直接连 PG）
"""
import pytest
from django.db import IntegrityError
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item, Title,
    locate_number, number_holding_status,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def cross_year_title(db):
    """《学报》：跨年卷 v.60，no.1 印 2023-10，no.3 跨 2023-12~2024-01。"""
    t = Title.objects.create(title="跨年报", issn="1111-2222")
    n1 = IssueNumber.objects.create(title=t, volume="60", number="1", sort_key=1)
    n2 = IssueNumber.objects.create(title=t, volume="60", number="2", sort_key=2)
    n3 = IssueNumber.objects.create(title=t, volume="60", number="3", sort_key=3)
    Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2023-10-01",
    ).numbers.add(n1)
    # 跨年卷：编号 v.60 no.3 只有一个，发行覆盖 2023-12 至 2024-01
    iss3 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR,
        issue_month="2023-12-01", issue_month_end="2024-01-01",
    )
    iss3.numbers.add(n3)
    Item.objects.create(
        barcode="CY-001", title=t, issue=Issue.objects.get(numbers=n1),
        location="期刊区A-01",
    )
    Item.objects.create(barcode="CY-003", title=t, issue=iss3, location="期刊区A-01")
    return t, {"n1": n1, "n2": n2, "n3": n3}


@pytest.fixture
def ceased_title(db):
    """《月报》：2024-06 停刊；no.5 正常出版，no.6 从未发行。"""
    t = Title.objects.create(
        title="停刊报",
        status=Title.PublicationStatus.CEASED, ceased_month="2024-06-01",
    )
    n5 = IssueNumber.objects.create(title=t, volume="12", number="5", sort_key=5)
    n6 = IssueNumber.objects.create(title=t, volume="12", number="6", sort_key=6)
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    # no.5 出版了但没有入库 → 缺藏；no.6 没有发行记录 → 缺号
    return t, {"n5": n5, "n6": n6}


@pytest.fixture
def combined_title(db):
    """《双月刊》：v.8 no.3-4 两期合刊，一个实物，两个编号关系。"""
    t = Title.objects.create(title="合刊报", issn="3333-4444")
    n3 = IssueNumber.objects.create(title=t, volume="8", number="3", sort_key=3)
    n4 = IssueNumber.objects.create(title=t, volume="8", number="4", sort_key=4)
    n5 = IssueNumber.objects.create(title=t, volume="8", number="5", sort_key=5)
    comb = Issue.objects.create(
        title=t, kind=Issue.IssueKind.COMBINED,
        issue_month="2024-03-01", issue_month_end="2024-04-01",
        note="3-4月合刊",
    )
    comb.numbers.set([n3, n4])
    item = Item.objects.create(
        barcode="CB-34", title=t, issue=comb, location="期刊区B-02",
    )
    return t, {"n3": n3, "n4": n4, "n5": n5}, item, comb


# ---------- 跨年卷 ----------

@pytest.mark.django_db
def test_cross_year_volume_one_number_spans_two_years(cross_year_title):
    t, nums = cross_year_title
    n3 = nums["n3"]
    issue = n3.issues.get()
    # 编号与发行年月分离：编号是 v.60 no.3，发行覆盖两个自然年
    assert (n3.volume, n3.number) == ("60", "3")
    assert str(issue.issue_month) == "2023-12-01"
    assert str(issue.issue_month_end) == "2024-01-01"
    # 按编号顺序而非月份排序，跨年卷仍是卷内连续位置
    ordered = list(
        IssueNumber.objects.filter(title=t).order_by("sort_key")
        .values_list("number", flat=True)
    )
    assert ordered == ["1", "2", "3"]


@pytest.mark.django_db
def test_cross_year_locate_by_number(cross_year_title, api):
    t, nums = cross_year_title
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=60&number=3")
    assert resp.status_code == 200
    data = resp.json()
    assert data["holding_status"] == "issued+held"
    assert len(data["matches"]) == 1
    assert data["matches"][0]["barcode"] == "CY-003"
    assert data["matches"][0]["location"] == "期刊区A-01"


# ---------- 停刊 + 缺号 vs 缺藏 ----------

@pytest.mark.django_db
def test_ceased_requires_month(db):
    from serials.serializers import TitleSerializer
    s = TitleSerializer(data={
        "title": "x", "status": "ceased",
    })
    assert not s.is_valid()
    assert "ceased_month" in s.errors


@pytest.mark.django_db
def test_missing_number_is_not_missing_holding(ceased_title):
    t, nums = ceased_title
    # no.5 已发行无实物 → 缺藏
    assert number_holding_status(t, nums["n5"]) == "issued+missing"
    # no.6 无发行记录 → 缺号，语义上不等于缺藏
    assert number_holding_status(t, nums["n6"]) == "ceased_gap"
    # 缺号槽位定位不到任何实物，但返回的是 404「缺号」而非「缺藏」
    rows = locate_number(nums["n6"])
    assert rows == []


@pytest.mark.django_db
def test_ceased_title_timeline(api, ceased_title):
    t, nums = ceased_title
    resp = api.get(f"/api/timeline/?title={t.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"]["status"] == "ceased"
    assert body["title"]["ceased_month"] == "2024-06-01"
    statuses = {s["number"]: s["holding_status"] for s in body["slots"]}
    assert statuses["5"] == "issued+missing"
    assert statuses["6"] == "ceased_gap"


# ---------- 两期合刊 ----------

@pytest.mark.django_db
def test_combined_issue_keeps_two_number_relations(combined_title):
    t, nums, item, comb = combined_title
    # 必须是两条编号关联，不能用一个条码覆盖多期号关系
    rels = IssueNumbering.objects.filter(issue=comb).order_by("number__number")
    assert list(rels.values_list("number__number", flat=True)) == ["3", "4"]
    # 实物只有一个，指向的是「这次合刊发行」，期号关系在关联表上
    assert Item.objects.filter(issue=comb).count() == 1
    assert item.barcode == "CB-34"


@pytest.mark.django_db
def test_combined_issue_rejects_single_number(db, api):
    t = Title.objects.create(title="z")
    n = IssueNumber.objects.create(title=t, volume="1", number="1", sort_key=1)
    resp = api.post("/api/issues/", {
        "title": t.id, "kind": "combined",
        "issue_month": "2024-03-01",
        "issue_month_end": "2024-04-01",
        "number_ids": [n.id],
    }, format="json")
    assert resp.status_code == 400
    assert "合刊" in str(resp.json())


@pytest.mark.django_db
def test_number_cannot_be_issued_twice(db, combined_title):
    t, nums, item, comb = combined_title
    # 编号已被合刊占用 → 拒绝再次登记发行
    from serials.serializers import IssueSerializer
    s = IssueSerializer(data={
        "title": t.id, "kind": "regular",
        "issue_month": "2024-09-01",
        "number_ids": [nums["n3"].id],
    })
    assert not s.is_valid()


@pytest.mark.django_db
def test_locate_combined_from_either_number(combined_title, api):
    t, nums, item, comb = combined_title
    for num in ("3", "4"):
        resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["holding_status"] == "issued+held"
        assert [m["barcode"] for m in data["matches"]] == ["CB-34"], num
    # no.5 已登记但未发行 → 缺号：200 + 空匹配，状态不同于缺藏
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["holding_status"] == "not_published"
    assert body["matches"] == []
    # 完全没登记过的编号才是 404
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=99")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_barcode_locates_both_numbers_of_combined(combined_title, api):
    resp = api.get("/api/items/locate/?barcode=CB-34")
    assert resp.status_code == 200
    match = resp.json()["matches"][0]
    assert {n["number"] for n in match["numbers"]} == {"3", "4"}


# ---------- 装订 / 拆订 ----------

@pytest.mark.django_db
def test_bind_combined_and_regular_then_locate(combined_title, cross_year_title, api):
    # 把跨年报 no.1 与合刊报... 不同刊不能混装；同刊内：
    t, nums, comb_item, comb = combined_title
    # 补一本 no.5 普通期实物（已发行）
    n5 = nums["n5"]
    iss5 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2024-05-01",
    )
    iss5.numbers.add(n5)
    item5 = Item.objects.create(
        barcode="CB-05", title=t, issue=iss5, location="期刊区B-09",
    )
    resp = api.post("/api/bindings/", {
        "call_number": "Q/HK-2024",
        "title": t.id,
        "location": "装订库 C-12",
        "bound_month": "2024-08-01",
        "item_ids": [comb_item.id, item5.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()
    assert Binding.objects.count() == 1

    comb_item.refresh_from_db()
    item5.refresh_from_db()
    assert comb_item.status == Item.ItemStatus.BOUND
    # 装订后实物自身位置字段不变，实际位置取装订册
    assert comb_item.location == "期刊区B-02"
    assert comb_item.current_location() == "装订库 C-12"

    # 合刊任一期号仍能找到，且位置指向装订册
    for num in ("3", "4", "5"):
        resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number={num}")
        assert resp.status_code == 200
        match = resp.json()["matches"][0]
        assert match["bound"] is True
        assert match["binding"] == "Q/HK-2024"
        assert match["location"] == "装订库 C-12"

    # 已装订实物不能重复装订
    resp = api.post("/api/bindings/", {
        "call_number": "Q/DUP", "title": t.id,
        "location": "X", "item_ids": [comb_item.id],
    }, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_bind_dedups_same_item_from_two_numbers(combined_title, api):
    """合刊实物会同时出现在 no.3 与 no.4 两个槽位：
    提交重复 item id 时应去重成功，而不是 500。"""
    t, nums, comb_item, _ = combined_title
    resp = api.post("/api/bindings/", {
        "call_number": "Q/DEDUP", "title": t.id, "location": "装订库 D-1",
        "item_ids": [comb_item.id, comb_item.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()
    assert len(resp.json()["items"]) == 1


@pytest.mark.django_db
def test_cannot_bind_across_titles(cross_year_title, combined_title, api):
    t1, n1 = cross_year_title
    t2, nums, comb_item, _ = combined_title
    cy_item = Item.objects.get(barcode="CY-001")
    resp = api.post("/api/bindings/", {
        "call_number": "Q/MIX", "title": t1.id,
        "location": "X", "item_ids": [cy_item.id, comb_item.id],
    }, format="json")
    assert resp.status_code == 400
    assert "混装" in str(resp.json())


@pytest.mark.django_db
def test_unbind_restores_locations(combined_title, api):
    t, nums, comb_item, comb = combined_title
    binding = Binding.objects.create(
        call_number="Q/HK-X", title=t, location="装订库 Z-1",
    )
    BindingEntry.objects.create(
        item=comb_item, binding=binding,
        previous_location=comb_item.location,
    )
    Item.objects.filter(id=comb_item.id).update(status=Item.ItemStatus.BOUND)

    resp = api.post("/api/bindings/unbind/", {"binding_id": binding.id},
                    format="json")
    assert resp.status_code == 200
    assert not Binding.objects.filter(id=binding.id).exists()
    assert not BindingEntry.objects.filter(item=comb_item).exists()

    comb_item.refresh_from_db()
    assert comb_item.status == Item.ItemStatus.AVAILABLE
    # 拆订后恢复各自位置
    assert comb_item.current_location() == "期刊区B-02"

    # 合刊的两个期号关系仍然完好，仍能从任一期号找到实物
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=8&number=4")
    match = resp.json()["matches"][0]
    assert match["barcode"] == "CB-34"
    assert match["bound"] is False
    assert match["location"] == "期刊区B-02"


# ---------- 入藏接口 ----------

@pytest.mark.django_db
def test_accession_item(api, cross_year_title):
    t, nums = cross_year_title
    n2 = nums["n2"]  # 缺号：先入藏会失败，因为没有发行期
    resp = api.post("/api/items/", {
        "barcode": "CY-002", "title": t.id,
        "issue": 999999, "location": "X",
    }, format="json")
    assert resp.status_code in (400,)

    iss2 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2023-11-01",
    )
    iss2.numbers.add(n2)
    resp = api.post("/api/items/", {
        "barcode": "CY-002", "title": t.id,
        "issue": iss2.id, "location": "期刊区A-02",
    }, format="json")
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["number_ids"] == [n2.id]
    assert body["current_location"] == "期刊区A-02"
