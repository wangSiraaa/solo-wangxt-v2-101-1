<template>
  <div class="panel">
    <h2>定位检索</h2>
    <div class="row">
      <label class="field"><b>卷</b>
        <input v-model="volume" placeholder="如 8（可空）" style="width: 110px" />
      </label>
      <label class="field"><b>期</b>
        <input v-model="number" placeholder="如 3" style="width: 90px"
               @keyup.enter="byNumber" />
      </label>
      <button @click="byNumber">按期号定位</button>
      <span style="width: 18px"></span>
      <label class="field"><b>条码</b>
        <input v-model="barcode" placeholder="SY-8-34" style="width: 130px"
               @keyup.enter="byBarcode" />
      </label>
      <button class="ghost" @click="byBarcode">按条码查</button>
    </div>

    <div v-if="error" class="msg err">{{ error }}</div>

    <div v-if="result" class="locate-result">
      <div class="row">
        <span class="badge" :class="badgeCls(result.holding_status)">
          {{ meta(result.holding_status).label }}
        </span>
        <span class="muted">{{ meta(result.holding_status).hint }}</span>
      </div>
      <p v-if="result.matches.length === 0" class="empty-hint">
        定位不到任何实物。若状态为「缺号」，表示没有发行记录，并非自动判定缺藏。
      </p>
      <div v-for="(m, i) in result.matches" :key="i" class="item-line">
        <code>{{ m.barcode }}</code>
        <span class="badge" :class="availMeta(m.availability).cls">
          {{ availMeta(m.availability).label }}
        </span>
        <span v-if="m.availability === 'overdue'" class="overdue-text">已逾期</span>
        <span class="loc">
          <template v-if="m.custody_kind === 'borrower'">
            🙋 {{ m.custody }}
            <span class="due">借出 {{ fmtDay(m.checkout_at) }} · 到期 {{ fmtDay(m.due_at) }}</span>
            <br /><span class="muted">归还架位：{{ m.location || "（未排架）" }}</span>
          </template>
          <template v-else>
            📍 {{ m.location || "（未排架）" }}
            <template v-if="m.bound">（装订册 {{ m.binding }}）</template>
          </template>
        </span>
        <CirculationActions
          :barcode="m.barcode"
          :availability="m.availability"
          :loan-id="m.active_loan_id"
          @changed="afterCircAction"
          @notice="(n) => (notice = n)"
        />
      </div>
      <p v-if="notice" class="msg" :class="notice.err ? 'err' : 'ok'">
        {{ notice.text }}
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";
import { HOLDING_STATUS, AVAILABILITY, fmtDay } from "../status.js";
import CirculationActions from "./CirculationActions.vue";

const props = defineProps({ titleId: [Number, String] });

const volume = ref("");
const number = ref("");
const barcode = ref("");
const result = ref(null);
const error = ref("");
const notice = ref(null);
const lastQuery = ref(null);

function availMeta(a) {
  return AVAILABILITY[a] || { label: a || "未知", cls: "gap" };
}
function meta(s) {
  return HOLDING_STATUS[s] || { label: s || "未登记", cls: "gap", hint: "" };
}
const badgeCls = (s) => meta(s).cls;

async function byNumber() {
  error.value = "";
  result.value = null;
  try {
    lastQuery.value = {
      kind: "number",
      params: { title: props.titleId, volume: volume.value, number: number.value },
    };
    result.value = await api.locate(lastQuery.value.params);
  } catch (e) {
    error.value = e.message;
  }
}
async function byBarcode() {
  error.value = "";
  result.value = null;
  try {
    lastQuery.value = { kind: "barcode", params: { barcode: barcode.value } };
    const data = await api.locate(lastQuery.value.params);
    const m = data.matches[0];
    result.value = m
      ? {
          // 遗失或缺藏为 missing；借出中仍是馆藏（held），但可用性单独展示
          holding_status:
            m.availability === "lost" ? "issued+missing" : "issued+held",
          matches: [m],
        }
      : { holding_status: "unregistered", matches: [] };
  } catch (e) {
    error.value = e.message;
  }
}

// 借还后自动用同一检索条件重新定位：三个入口状态保持联动
async function afterCircAction() {
  notice.value = null;
  const q = lastQuery.value;
  if (!q) return;
  try {
    const data = await api.locate(q.params);
    if (q.kind === "barcode") {
      const m = data.matches[0];
      result.value = m
        ? {
            holding_status:
              m.availability === "lost" ? "issued+missing" : "issued+held",
            matches: [m],
          }
        : { holding_status: "unregistered", matches: [] };
    } else {
      result.value = data;
    }
  } catch (e) {
    error.value = e.message;
  }
}
</script>
