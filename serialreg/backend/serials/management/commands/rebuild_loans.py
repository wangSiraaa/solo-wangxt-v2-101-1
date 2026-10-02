"""从流通事件链重放，重建所有借出单与实体的物化状态。

用途：模拟服务刷新/重启后的恢复，或修复被外部改乱的 Loan/Item 状态。
逾期不入库、无需重放；完整历史始终保留在 serials_loanevent 表中。
"""
from django.core.management.base import BaseCommand

from serials.circulation import rebuild_loan_state
from serials.models import Loan


class Command(BaseCommand):
    help = "按 LoanEvent 事件链重放所有借出单的当前状态（刷新/重启恢复）"

    def add_arguments(self, parser):
        parser.add_argument("--loan-id", type=int, default=None,
                            help="只重建指定借出单；缺省则全量重建")

    def handle(self, *args, **options):
        if options["loan_id"]:
            if not Loan.objects.filter(pk=options["loan_id"]).exists():
                self.stderr.write(f"借出单 {options['loan_id']} 不存在。")
                return
            loans = list(rebuild_loan_state(
                loan=Loan.objects.get(pk=options["loan_id"])))
        else:
            loans = rebuild_loan_state()
        for loan in loans:
            self.stdout.write(
                f"  Loan#{loan.id} {loan.item.barcode} → {loan.get_status_display()}"
                f"（version={loan.version}）"
            )
        self.stdout.write(self.style.SUCCESS(
            f"✓ 已按事件链重建 {len(loans)} 张借出单。"))
