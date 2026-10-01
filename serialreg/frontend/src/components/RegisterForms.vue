<template>
  <div class="panel">
    <h2>登记操作</h2>

    <h3>① 登记期号槽位（卷期编号，与发行年月分开）</h3>
    <div class="row">
      <label class="field"><b>卷</b>
        <input v-model="num.volume" placeholder="60（可空）" style="width: 90px" />
      </label>
      <label class="field"><b>期</b>
        <input v-model="num.number" placeholder="3" style="width: 80px" />
      </label>
      <label class="field"><b>排序键</b>
        <input v-model.number="num.sort_key" type="number" style="width: 90px" />
      </label>
      <button @click="addNumber">新增编号</button>
    </div>
    <p class="muted">先有编号槽位才谈「缺号」。编号本身不含年月，跨年卷按卷+期唯一。</p>

    <h3>② 登记发行期（普通期 / 两期合刊）</h3>
    <div class="row">
      <label class="field"><b>类型</b>
        <select v-model="iss.kind" @change="iss.numbers = []">
          <option value="regular">普通期（1 个期号）</option>
          <option value="combined">两期合刊（≥2 个期号）</option>
        </select>
      </label>
      <label class="field"><b>发行年月</b>
        <input v-model="iss.issue_month" type="month" />
      </label>
      <label class="field"><b>截止年月</b>
        <input v-model="iss.issue_month_end" type="month" />
      </label>
    </div>
    <div class="checks" style="margin-top:6px">
      <label v-for="s in freeSlots" :key="s.number_id">
        <input type="checkbox" :value="s.number_id" v-model="iss.numbers" />
        v.{{ s.volume || "—" }} no.{{ s.number }}
      </label>
      <span v-if="freeSlots.length === 0" class="muted">
        没有可关联的空闲编号，请先登记编号
      </span>
    </div>
    <div class="row" style="margin-top:8px">
      <button @click="addIssue">
        {{ iss.kind === "combined" ? "登记合刊发行" : "登记普通期发行" }}
      </button>
      <span class="muted">
        合刊会为每个期号建立独立关联记录，不是用一个条码覆盖多个期号。
      </span>
    </div>

    <h3>③ 入藏实物（一条条码 = 一个实体）</h3>
    <div class="row">
      <label class="field"><b>条码</b>
        <input v-model="item.barcode" placeholder="NJ-60-2" style="width: 130px" />
      </label>
      <label class="field"><b>对应发行期</b>
        <select v-model="item.issue" style="min-width: 230px">
          <option :value="null">请选择已登记的发行期</option>
          <option v-for="i in allIssues" :key="i.issue_id" :value="i.issue_id">
            {{ monthLabel(i) }} — {{ i.combined_numbers.map(n => `v.${n.volume||"—"}no.${n.number}`).join("+") }}
            {{ i.kind === "combined" ? "（合刊）" : "" }}
          </option>
        </select>
      </label>
      <label class="field"><b>馆藏位置</b>
        <input v-model="item.location" placeholder="现刊区 A-02" style="width: 140px" />
      </label>
      <button @click="addItem">入藏</button>
    </div>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from "vue";
import { api } from "../api.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});
const emit = defineEmits(["changed"]);

const msg = ref(null);
function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 4000);
}

const num = reactive({ volume: "", number: "", sort_key: 0 });
const iss = reactive({
  kind: "regular", issue_month: "", issue_month_end: "", numbers: [],
});
const item = reactive({ barcode: "", issue: null, location: "" });

// 尚未被任何发行期占用的编号槽位
const freeSlots = computed(() =>
  (props.timeline?.slots || []).filter((s) => s.issues.length === 0),
);
const allIssues = computed(() => {
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const i of s.issues) if (!out.some((x) => x.issue_id === i.issue_id)) out.push(i);
  }
  return out;
});
function monthLabel(i) {
  const a = i.issue_month?.slice(0, 7);
  const b = i.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a}~${b}` : a;
}

async function addNumber() {
  if (!num.number) return notify("期号必填。", true);
  try {
    await api.createNumber({
      title: props.titleId,
      volume: num.volume, number: num.number, sort_key: num.sort_key,
    });
    num.volume = ""; num.number = ""; num.sort_key = 0;
    notify("期号槽位已登记。");
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function addIssue() {
  if (!iss.issue_month) return notify("请填写发行年月。", true);
  if (iss.kind === "combined" && iss.numbers.length < 2)
    return notify("两期合刊必须勾选至少两个期号。", true);
  if (iss.kind === "regular" && iss.numbers.length !== 1)
    return notify("普通期只能勾选一个期号。", true);
  try {
    await api.createIssue({
      title: props.titleId,
      kind: iss.kind,
      issue_month: iss.issue_month ? `${iss.issue_month}-01` : null,
      issue_month_end: iss.issue_month_end ? `${iss.issue_month_end}-01` : null,
      number_ids: iss.numbers,
    });
    iss.kind = "regular"; iss.issue_month = "";
    iss.issue_month_end = ""; iss.numbers = [];
    notify("发行期已登记，编号与发行关系已建立。");
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function addItem() {
  if (!item.barcode || !item.issue)
    return notify("条码和对应发行期必填。", true);
  try {
    await api.createItem({
      barcode: item.barcode, title: props.titleId,
      issue: item.issue, location: item.location,
    });
    item.barcode = ""; item.issue = null; item.location = "";
    notify("实物已入藏。");
    emit("changed");
  } catch (e) { notify(e.message, true); }
}
</script>
