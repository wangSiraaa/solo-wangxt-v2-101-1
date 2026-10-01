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
        <span class="badge" :class="m.status === 'lost' ? 'missing' : 'ok'">
          {{ itemStatus[m.status] || m.status }}
        </span>
        <span class="loc">
          📍 {{ m.location || "（未排架）" }}
          <template v-if="m.bound">（装订册 {{ m.binding }}）</template>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";
import { HOLDING_STATUS, ITEM_STATUS } from "../status.js";

const props = defineProps({ titleId: [Number, String] });

const volume = ref("");
const number = ref("");
const barcode = ref("");
const result = ref(null);
const error = ref("");
const itemStatus = ITEM_STATUS;

function meta(s) {
  return HOLDING_STATUS[s] || { label: s || "未登记", cls: "gap", hint: "" };
}
const badgeCls = (s) => meta(s).cls;

async function byNumber() {
  error.value = "";
  result.value = null;
  try {
    result.value = await api.locate({
      title: props.titleId, volume: volume.value, number: number.value,
    });
  } catch (e) {
    error.value = e.message;
  }
}
async function byBarcode() {
  error.value = "";
  result.value = null;
  try {
    const data = await api.locate({ barcode: barcode.value });
    const m = data.matches[0];
    result.value = m
      ? {
          holding_status:
            m.status === "lost" ? "issued+missing" : "issued+held",
          matches: [m],
        }
      : { holding_status: "unregistered", matches: [] };
  } catch (e) {
    error.value = e.message;
  }
}
</script>
