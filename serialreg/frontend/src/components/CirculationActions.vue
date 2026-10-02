<template>
  <span class="circ">
    <!-- 在馆未装订：可借出 -->
    <template v-if="availability === 'available'">
      <button class="tiny" @click="openCheckout" title="借出该实体">借出</button>
      <span v-if="checking" class="checkout-inline">
        <input v-model="borrower" placeholder="借阅人" class="tiny-input"
               @keyup.enter="doCheckout" />
        <input v-model="dueDate" type="date" class="tiny-input"
               :title="`到期日（默认 ${defaultDue}）`" />
        <button class="tiny" @click="doCheckout" :disabled="submitting">确认</button>
        <button class="tiny ghost" @click="checking = false">取消</button>
      </span>
    </template>

    <!-- 借出中 / 逾期：归还、报失 -->
    <template v-else-if="availability === 'checked_out' || availability === 'overdue'">
      <button class="tiny ok" @click="doReturn" :disabled="submitting">归还</button>
      <button class="tiny danger" @click="doLost" :disabled="submitting"
              title="登记借出后遗失（流通事件）">报失</button>
      <span v-if="loanId" class="loan-ref">单 #{{ loanId }}</span>
    </template>

    <!-- 借出后遗失：可补登记归还（如找回）仍走事件，迟到会留痕不改状态 -->
    <template v-else-if="availability === 'lost' && loanId">
      <button class="tiny ghost" @click="doReturn" :disabled="submitting"
              title="找回：仍作为归还事件提交；若晚于报失将被拒绝">
        找回登记归还
      </button>
      <span class="loan-ref">单 #{{ loanId }}</span>
    </template>
  </span>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";

const props = defineProps({
  barcode: String,
  availability: String,
  loanId: [Number, String],
});
const emit = defineEmits(["changed", "notice"]);

const checking = ref(false);
const submitting = ref(false);
const borrower = ref("");
const defaultDue = new Date(Date.now() + 14 * 86400000)
  .toISOString().slice(0, 10);
const dueDate = ref(defaultDue);

function notify(text, err = false) {
  emit("notice", { text, err });
}

function openCheckout() {
  checking.value = true;
}

async function doCheckout() {
  if (!dueDate.value) return notify("请选择到期日。", true);
  submitting.value = true;
  try {
    const data = await api.checkout({
      barcode: props.barcode,
      borrower: borrower.value || "匿名读者",
      due_at: `${dueDate.value}T00:00:00`,
    });
    notify(`已借出，借出单 #${data.id}，到期 ${dueDate.value}。同一 event_id 重放不会重复借出。`);
    checking.value = false;
    borrower.value = "";
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  } finally {
    submitting.value = false;
  }
}

async function doReturn() {
  submitting.value = true;
  try {
    const data = await api.returnLoan(
      props.loanId ? { loan: props.loanId } : { barcode: props.barcode },
    );
    const er = data.event_result;
    if (er.replayed) notify("归还请求重放：该事件已存在，未重复改变状态。");
    else notify(`已归还，实物恢复到原架位（${data.shelf_location || "未排架"}）。`);
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  } finally {
    submitting.value = false;
  }
}

async function doLost() {
  if (!confirm("确认登记该实物遗失？此为流通事件，会进入事件链审计。")) return;
  submitting.value = true;
  try {
    const data = await api.reportLoanLost(
      props.loanId ? { loan: props.loanId } : { barcode: props.barcode },
    );
    const er = data.event_result;
    if (er.superseded) notify(`迟到的遗失事件已留痕，但未覆盖较新处置。${er.reject_reason}`, true);
    else notify("已登记遗失（借出单状态：遗失）。");
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  } finally {
    submitting.value = false;
  }
}
</script>
