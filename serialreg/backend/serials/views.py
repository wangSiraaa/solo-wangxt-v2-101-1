from django.db.models import Exists, OuterRef, Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .circulation import CirculationError, apply_event, checkout_item
from .models import (
    Binding, Issue, IssueNumber, IssueNumbering, Item, Loan, Title,
    circulation_info, locate_number, number_holding_status, open_loan_prefetch,
)
from .serializers import (
    BindingSerializer, CheckoutSerializer, CirculationEventSerializer,
    IssueSerializer, ItemSerializer, IssueNumberSerializer, LoanEventSerializer,
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
    ).prefetch_related("issue__numbers", open_loan_prefetch())
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置，
        借出中返回流通保管位置、到期日与逾期标记。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            result = []
            for it in items:
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
                    "availability": it.availability(),
                    "circulation": circulation_info(it),
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


class LoanViewSet(viewsets.GenericViewSet):
    """本地流通单据：开单（借出）、追加事件（归还/逾期/遗失）、查询事件链。

    所有写操作都幂等：同一 idempotency_key 重放返回原结果，不重复改变状态。
    """

    queryset = Loan.objects.select_related(
        "item", "item__title",
    ).prefetch_related("events")
    serializer_class = LoanSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("title"):
            qs = qs.filter(item__title_id=params["title"])
        if params.get("item"):
            qs = qs.filter(item_id=params["item"])
        if params.get("barcode"):
            qs = qs.filter(item__barcode=params["barcode"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs

    def list(self, request):
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, pk=None):
        return Response(self.get_serializer(self.get_object()).data)

    def create(self, request):
        """开单借出。同一 idempotency_key 重放 → 200 返回原单据。"""
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            loan, replayed = checkout_item(
                item=data["item"],
                idempotency_key=data["idempotency_key"],
                due_date=data["due_date"],
                custody_location=data.get("custody_location", ""),
                occurred_at=data.get("occurred_at"),
            )
        except CirculationError as e:
            return Response(
                {"detail": str(e)},
                status=self._error_status(e),
            )
        body = LoanSerializer(loan).data
        body["replayed"] = replayed
        return Response(
            body,
            status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get", "post"])
    def events(self, request, pk=None):
        """GET 查看事件链；POST 追加归还/逾期/遗失事件。

        迟到事件（occurred_at 早于最近已应用事件）记录为 applied=False，
        不覆盖较新的处置；重放同一 idempotency_key 不产生第二条事件。
        """
        loan = self.get_object()
        if request.method == "GET":
            return Response(
                CirculationEventSerializer(loan.events.all(), many=True).data,
            )
        serializer = LoanEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            event, replayed = apply_event(
                loan_id=loan.pk,
                event_type=data["event_type"],
                idempotency_key=data["idempotency_key"],
                occurred_at=data.get("occurred_at"),
                note=data.get("note", ""),
            )
        except CirculationError as e:
            return Response(
                {"detail": str(e)},
                status=self._error_status(e),
            )
        return Response({
            "event": CirculationEventSerializer(event).data,
            "loan": LoanSerializer(loan.__class__.objects
                                   .prefetch_related("events")
                                   .get(pk=loan.pk)).data,
            "replayed": replayed,
        }, status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED)

    @staticmethod
    def _error_status(exc):
        if exc.code == "conflict":
            return status.HTTP_409_CONFLICT
        return status.HTTP_400_BAD_REQUEST


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
                            ).prefetch_related(open_loan_prefetch()),
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
                            {
                                "item_id": it.id,
                                "barcode": it.barcode,
                                "status": it.status,
                                "availability": it.availability(),
                                "location": it.current_location(),
                                "bound": it.is_bound,
                                "binding": it.binding_entry.binding.call_number
                                if it.is_bound else None,
                                "circulation": circulation_info(it),
                            }
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
