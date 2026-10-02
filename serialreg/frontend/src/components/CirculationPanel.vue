<template>
  <div class="panel">
    <h2>本地流通</h2>

    <h3>① 开单借出（仅「在馆且未装订」的实物可借）</h3>
    <div class="row">
      <label class="field"><b>实物条码</b>
        <select v-model="form.item" style="min-width: 170px">
          <option :value="null">选择可借实物</option>
          <option v-for="it in loanableItems" :key="it.item_pk" :value="it.item_pk">
            {{ it.barcode }} — {{ it.location || "（未排架）" }}
          </option>
        </select>
      </label>
      <label class="field"><b>到期日</b>
        <input v-model="form.due_date" type="date" />
      </label>
      <label class="field"><b>借出保管位置</b>
        <input v-model="form.custody_location" placeholder="流通台" style="width: 120px" />
      </label>
      <button @click="doCheckout" :disabled="!form.item || pending">借出开单</button>
    </div>
    <p class="muted">
      本次开单幂等标识 <code>{{ form.idempotency_key }}</code>
      <button class="tiny ghost" @click="resetKey">换一个新的</button>
      —— 网络重试/重复提交同一标识只会命中同一张单据。
    </p>

    <h3 v-if="openLoans.length">② 在借单据（{{ openLoans.length }}）</h3>
    <div v-for="ln in openLoans" :key="ln.id" class="loan-card">
      <div class="row">
        <code>{{ ln.barcode }}</code>
        <span class="badge loaned">在借</span>
        <span v-if="ln.overdue" class="badge missing">已逾期</span>
        <span class="loc">到期 {{ ln.due_date }} · 保管于 {{ ln.custody_location }}</span>
        <button class="tiny" @click="doEvent(ln, 'return')" :disabled="pending">归还</button>
        <button class="tiny ghost" @click="doEvent(ln, 'overdue')" :disabled="pending">
          标记逾期
        </button>
        <button class="tiny danger" @click="doEvent(ln, 'lost')" :disabled="pending">
          遗失
        </button>
        <button class="tiny ghost" @click="toggle(ln.id)">
          {{ expanded[ln.id] ? "收起事件链" : "事件链" }}
        </button>
      </div>
      <EventChain v-if="expanded[ln.id]" :events="ln.events" />
    </div>

    <h3 v-if="closedLoans.length">③ 已关闭单据（{{ closedLoans.length }}）</h3>
    <div v-for="ln in closedLoans" :key="ln.id" class="loan-card closed">
      <div class="row">
        <code>{{ ln.barcode }}</code>
        <span class="badge" :class="ln.close_reason === 'lost' ? 'missing' : 'ok'">
          {{ ln.close_reason === "lost" ? "遗失关闭" : "已归还" }}
        </span>
        <span class="loc">到期 {{ ln.due_date }}</span>
        <button class="tiny ghost" @click="toggle(ln.id)">
          {{ expanded[ln.id] ? "收起事件链" : "事件链" }}
        </button>
      </div>
      <EventChain v-if="expanded[ln.id]" :events="ln.events" />
    </div>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, h, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import { LOAN_EVENT } from "../status.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});
const emit = defineEmits(["changed"]);

const openLoans = ref([]);
const closedLoans = ref([]);
const expanded = reactive({});
const pending = ref(false);
const msg = ref(null);

function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 5000);
}

const newKey = () =>
  (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

const form = reactive({
  item: null,
  due_date: "",
  custody_location: "流通台",
  idempotency_key: newKey(),
});
// 每个「单据+动作」在未成功前复用同一幂等键：重试不会产生第二个事件
const actionKeys = reactive({});

function resetKey() {
  form.idempotency_key = newKey();
}

// 从时间轴扁平出可借实物（合刊实物按主键去重）
const loanableItems = computed(() => {
  const seen = new Set();
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const iss of s.issues) {
      for (const it of iss.items) {
        if (it.availability !== "available") continue;
        const pk = it.item_id ?? it.barcode;
        if (seen.has(pk)) continue;
        seen.add(pk);
        out.push({ item_pk: pk, barcode: it.barcode, location: it.location });
      }
    }
  }
  return out;
});

async function loadLoans() {
  if (!props.titleId) return;
  try {
    const [open, closed] = await Promise.all([
      api.listLoans({ title: props.titleId, status: "open" }),
      api.listLoans({ title: props.titleId, status: "closed" }),
    ]);
    openLoans.value = open;
    closedLoans.value = closed.slice(0, 10);
  } catch (e) { /* 列表非关键路径 */ }
}
watch(() => props.titleId, loadLoans, { immediate: true });

async function doCheckout() {
  if (!form.due_date) return notify("请填写到期日。", true);
  pending.value = true;
  try {
    const data = await api.checkout({
      item: form.item,
      due_date: form.due_date,
      custody_location: form.custody_location || "流通台",
      idempotency_key: form.idempotency_key,
    });
    notify(data.replayed
      ? `重复提交被幂等去重：命中原流通单 #${data.id}。`
      : `已开单 #${data.id}，${data.barcode} 借出，到期 ${data.due_date}。`);
    form.item = null;
    resetKey();
    await loadLoans();
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  } finally {
    pending.value = false;
  }
}

async function doEvent(loan, type) {
  const k = `${loan.id}:${type}`;
  actionKeys[k] = actionKeys[k] || newKey();
  pending.value = true;
  try {
    const data = await api.postLoanEvent(loan.id, {
      event_type: type,
      idempotency_key: actionKeys[k],
    });
    const ev = data.event;
    notify(data.replayed
      ? `重复提交被幂等去重：${LOAN_EVENT[type]}事件已存在（#${ev.seq}）。`
      : ev.applied
        ? `${LOAN_EVENT[type]}已登记（事件 #${ev.seq}）。`
        : `迟到事件已留痕（#${ev.seq}），未覆盖较新的处置。`);
    delete actionKeys[k];
    await loadLoans();
    emit("changed");
  } catch (e) {
    notify(e.message, true);
  } finally {
    pending.value = false;
  }
}

function toggle(id) {
  expanded[id] = !expanded[id];
}

// 事件链子组件：按 seq 展示审计历史，迟到事件标注「未应用」
const EventChain = (props) =>
  h("ol", { class: "event-chain" },
    props.events.map((e) =>
      h("li", { key: e.id, class: e.applied ? "" : "stale" }, [
        h("span", { class: "seq" }, `#${e.seq}`),
        h("b", null, LOAN_EVENT[e.event_type] || e.event_type),
        h("span", { class: "muted" },
          ` 发生于 ${e.occurred_at?.slice(0, 16).replace("T", " ")}`),
        e.applied
          ? null
          : h("span", { class: "badge gap" }, "迟到·未应用"),
        e.note ? h("span", { class: "muted" }, ` ${e.note}`) : null,
      ])));
EventChain.props = ["events"];
</script>
