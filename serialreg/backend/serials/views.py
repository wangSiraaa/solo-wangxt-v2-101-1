from django.db.models import Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import circulation
from .circulation import CirculationError
from .models import (
    Binding, Issue, IssueNumber, IssueNumbering, Item,
    Loan, LoanEvent, Title,
    ACTIVE_LOAN_PREFETCH, circulation_snapshot,
    locate_number, number_holding_status,
)
from .serializers import (
    BindingSerializer, CheckoutSerializer, IssueSerializer, ItemSerializer,
    IssueNumberSerializer, LoanEventPostSerializer, LoanEventSerializer,
    LoanSerializer, TitleSerializer, UnbindSerializer,
)


class TitleViewSet(viewsets.ModelViewSet):
    queryset = Title.objects.all()
    serializer_class = TitleSerializer


class IssueNumberViewSet(viewsets.ModelViewSet):
    queryset = IssueNumber.objects.all()
    serializer_class = IssueNumberSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.prefetch_related(
        "numberings__number", "items",
    ).select_related("title")
    serializer_class = IssueSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.select_related(
        "title", "issue", "binding_entry__binding",
    ).prefetch_related("issue__numbers", ACTIVE_LOAN_PREFETCH)
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置；
        借出中返回实际可得性、到期日与当前保管位置（借阅人）。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            result = []
            for it in items:
                snap = circulation_snapshot(it)
                result.append({
                    "barcode": it.barcode,
                    "issue_id": it.issue_id,
                    "numbers": [
                        {"volume": n.volume, "number": n.number}
                        for n in it.issue.numbers.all()
                    ],
                    "location": it.current_location(),
                    "bound": it.is_bound,
                    "binding": it.binding_entry.binding.call_number
                    if it.is_bound else None,
                    "status": it.status,
                    "availability": snap["availability"],
                    "custody": snap["custody"],
                    "custody_kind": snap["custody_kind"],
                    "active_loan_id": snap["active_loan_id"],
                    "borrower": snap["borrower"],
                    "checkout_at": snap["checkout_at"],
                    "due_at": snap["due_at"],
                })
            return Response({"query": {"barcode": barcode}, "matches": result})

        if not (title_id and number):
            return Response(
                {"detail": "需要提供 barcode，或同时提供 title 与 number。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = IssueNumber.objects.filter(
            title_id=title_id, number=number,
        )
        if volume != "":
            qs = qs.filter(volume=volume)
        try:
            issue_number = qs.get()
        except IssueNumber.DoesNotExist:
            # 编号本身未登记：区别于「已登记但无发行」的缺号
            return Response({
                "detail": "该卷期编号未在馆藏系统登记。",
                "holding_status": "unregistered",
                "matches": [],
            }, status=status.HTTP_404_NOT_FOUND)
        except IssueNumber.MultipleObjectsReturned:
            return Response(
                {"detail": "卷/期定位到多条编号，请补全卷号。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        matches = locate_number(issue_number)
        # 缺号（无发行记录）是正常业务状态，返回 200，不自动等同缺藏
        return Response({
            "query": {"title": title_id, "volume": volume, "number": number},
            "holding_status": number_holding_status(issue_number.title, issue_number),
            "matches": matches,
        })


class BindingViewSet(viewsets.ModelViewSet):
    queryset = Binding.objects.prefetch_related(
        "entries__item",
    ).select_related("title")
    serializer_class = BindingSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs

    @action(detail=False, methods=["post"])
    def unbind(self, request):
        serializer = UnbindSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.save()
        return Response({
            "detail": "拆订完成，各实物已恢复原位置。",
            "restored": [
                {"barcode": it.barcode, "location": it.location,
                 "status": it.status}
                for it in items
            ],
        })


class LoanViewSet(viewsets.ReadOnlyModelViewSet):
    """流通单据（借出单）及其事件链。写操作走 checkout/return/lost 动作。"""

    queryset = (
        Loan.objects.select_related("item")
        .prefetch_related("events")
    )
    serializer_class = LoanSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("item"):
            qs = qs.filter(item_id=params["item"])
        if params.get("barcode"):
            qs = qs.filter(item__barcode=params["barcode"])
        if params.get("title"):
            qs = qs.filter(item__title_id=params["title"])
        if params.get("active") in ("1", "true", "yes"):
            qs = qs.filter(status__in=Loan.ACTIVE_STATUSES)
        return qs

    def _dispatch(self, request, event_type=None):
        """checkout / return / lost 的公共入口，统一映射业务异常。"""
        if event_type is None:  # checkout
            serializer = CheckoutSerializer(data=request.data)
        else:
            serializer = LoanEventPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if event_type is None:
                result = circulation.checkout(
                    item_id=getattr(data.get("item"), "id", None),
                    barcode=data.get("barcode"),
                    borrower=data.get("borrower", ""),
                    due_at=data["due_at"],
                    occurred_at=data.get("occurred_at"),
                    event_id=str(data["event_id"]) if data.get("event_id") else None,
                    actor=data.get("actor", ""),
                    note=data.get("note", ""),
                )
            else:
                result = circulation.apply_event(
                    event_type,
                    loan_id=getattr(data.get("loan"), "id", None),
                    item_id=getattr(data.get("item"), "id", None),
                    barcode=data.get("barcode"),
                    occurred_at=data.get("occurred_at"),
                    event_id=str(data["event_id"]) if data.get("event_id") else None,
                    actor=data.get("actor", ""),
                    note=data.get("note", ""),
                )
        except CirculationError as exc:
            code_map = {
                "not_found": status.HTTP_404_NOT_FOUND,
                "bad_request": status.HTTP_400_BAD_REQUEST,
            }
            http_status = code_map.get(
                exc.code, status.HTTP_409_CONFLICT,
            )
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=http_status,
            )
        body = LoanSerializer(result["loan"]).data
        body["event_result"] = {
            "replayed": result["replayed"],
            "applied": result["applied"],
            "superseded": result["superseded"],
            "rejected": result["rejected"],
            "event_id": str(result["event"].event_id),
            "seq": result["event"].seq,
            "reject_reason": result["event"].reject_reason,
        }
        return Response(body, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def checkout(self, request):
        return self._dispatch(request)

    @action(detail=False, methods=["post"])
    def return_item(self, request):
        return self._dispatch(request, LoanEvent.EventType.RETURN)

    @action(detail=False, methods=["post"])
    def lost(self, request):
        return self._dispatch(request, LoanEvent.EventType.LOST)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        """单张借出单的完整事件链（含只留痕的迟到/非法事件）。"""
        loan = self.get_object()
        events = loan.events.all()
        return Response({
            "loan": loan.id,
            "status": loan.status,
            "derived_status": loan.derived_status(),
            "events": LoanEventSerializer(events, many=True).data,
        })

    @action(detail=True, methods=["post"])
    def rebuild(self, request, pk=None):
        """按事件链重放物化当前状态（模拟刷新/重启后的恢复）。"""
        loan = self.get_object()
        circulation.rebuild_loan_state(loan=loan)
        loan.refresh_from_db()
        return Response(LoanSerializer(loan).data)


class TimelineViewSet(viewsets.ViewSet):
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记。"""

    def list(self, request):
        title_id = request.query_params.get("title")
        if not title_id:
            return Response(
                {"detail": "需要提供 title 参数。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = Title.objects.get(pk=title_id)
        numbers = (
            IssueNumber.objects.filter(title=title)
            .prefetch_related(
                Prefetch(
                    "issues",
                    queryset=Issue.objects.prefetch_related(
                        Prefetch(
                            "numberings",
                            queryset=IssueNumbering.objects.select_related("number"),
                        ),
                        Prefetch(
                            "items",
                            queryset=Item.objects.select_related(
                                "binding_entry__binding",
                            ).prefetch_related(ACTIVE_LOAN_PREFETCH),
                        ),
                    ),
                ),
            )
            .order_by("sort_key", "id")
        )
        slots = []
        for n in numbers:
            issues = list(n.issues.all())
            items = [it for iss in issues for it in iss.items.all()]
            slots.append({
                "number_id": n.id,
                "volume": n.volume,
                "number": n.number,
                "holding_status": number_holding_status(title, n),
                "issues": [
                    {
                        "issue_id": iss.id,
                        "kind": iss.kind,
                        "issue_month": iss.issue_month,
                        "issue_month_end": iss.issue_month_end,
                        "label": "·".join(
                            f"{nn.number.volume}({nn.number.number})"
                            for nn in iss.numberings.all()
                        ),
                        "combined_numbers": [
                            {"volume": nn.number.volume, "number": nn.number.number}
                            for nn in iss.numberings.all()
                        ],
                        "items": [
                            self._item_payload(it)
                            for it in iss.items.all()
                        ],
                    }
                    for iss in issues
                ],
            })
        return Response({
            "title": TitleSerializer(title).data,
            "slots": slots,
        })

    @staticmethod
    def _item_payload(it):
        snap = circulation_snapshot(it)
        return {
            "item_id": it.id,
            "barcode": it.barcode,
            "status": it.status,
            "location": it.current_location(),
            "bound": it.is_bound,
            "binding": it.binding_entry.binding.call_number
            if it.is_bound else None,
            # 流通层：实际可得性 / 当前保管位置 / 到期日
            "availability": snap["availability"],
            "custody": snap["custody"],
            "custody_kind": snap["custody_kind"],
            "active_loan_id": snap["active_loan_id"],
            "borrower": snap["borrower"],
            "checkout_at": snap["checkout_at"],
            "due_at": snap["due_at"],
        }
