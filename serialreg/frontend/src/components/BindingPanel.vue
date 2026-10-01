<template>
  <div class="panel">
    <h2>装订与拆订</h2>

    <div class="row">
      <label class="field"><b>装订索书号</b>
        <input v-model="form.call_number" placeholder="Q/SY-2025" style="width: 150px" />
      </label>
      <label class="field"><b>装订后位置</b>
        <input v-model="form.location" placeholder="装订库 C-13" style="width: 150px" />
      </label>
      <label class="field"><b>装订月份</b>
        <input v-model="form.bound_month" type="month" />
      </label>
      <button @click="doBind" :disabled="picked.length === 0">装订所选实物</button>
    </div>

    <div class="checks" style="margin-top:8px">
      <span class="muted">仅可勾选同一种刊中<b>未装订</b>的实物：</span><br />
      <label v-for="it in unboundItems" :key="it.barcode">
        <input type="checkbox" :value="it.item_pk" v-model="picked" />
        <code>{{ it.barcode }}</code>
        <span class="loc">{{ it.location || "（未排架）" }}</span>
      </label>
      <span v-if="unboundItems.length === 0" class="muted">
        （暂无可装订实物；已装订实物必须先拆订）
      </span>
    </div>
    <p class="muted" style="margin-top:6px">
      装订后各实物条码与合刊的多期号关系都保留，实际位置改指向装订册；
      系统会记住每个实物装订前的位置。
    </p>

    <h3 v-if="bindings.length">现有装订册</h3>
    <div class="binding-list">
      <div v-for="b in bindings" :key="b.id" class="binding-card">
        <div class="row">
          <strong>{{ b.call_number }}</strong>
          <span class="loc">📍 {{ b.location }}</span>
          <span v-if="b.bound_month" class="muted">{{ b.bound_month.slice(0, 7) }} 装订</span>
          <button class="tiny danger" @click="doUnbind(b.id)">拆订（恢复各自位置）</button>
        </div>
        <div class="muted" style="margin-top:4px">
          内含：
          <span v-for="(it, i) in b.items" :key="it.barcode">
            <code>{{ it.barcode }}</code>
            <span title="装订前位置">原位于 {{ it.previous_location || "（未排架）" }}</span>
            <span v-if="i < b.items.length - 1">；</span>
          </span>
        </div>
      </div>
    </div>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});
const emit = defineEmits(["changed"]);

const form = reactive({ call_number: "", location: "", bound_month: "" });
const picked = ref([]);
const bindings = ref([]);
const msg = ref(null);

function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 4000);
}

// 从时间轴扁平出未装订实物。合刊实物会挂在多个期号槽位下，必须按主键去重。
const unboundItems = computed(() => {
  const seen = new Set();
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const iss of s.issues) {
      for (const it of iss.items) {
        if (it.bound) continue;
        const pk = it.item_id ?? it.barcode;
        if (seen.has(pk)) continue; // 合刊：同一实物只列一次
        seen.add(pk);
        out.push({ item_pk: pk, barcode: it.barcode, location: it.location });
      }
    }
  }
  return out;
});

async function loadBindings() {
  try {
    bindings.value = await api.listBindings(props.titleId);
  } catch (e) { /* 列表非关键路径 */ }
}
watch(() => props.titleId, loadBindings, { immediate: true });

async function doBind() {
  try {
    await api.bind({
      call_number: form.call_number,
      title: props.titleId,
      location: form.location,
      bound_month: form.bound_month ? `${form.bound_month}-01` : null,
      item_ids: picked.value.map((v) =>
        typeof v === "number" ? v : Number(v)),
    });
    form.call_number = ""; form.location = ""; form.bound_month = "";
    picked.value = [];
    notify("装订完成。从任一期号检索，位置均指向装订册。");
    await loadBindings();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function doUnbind(id) {
  try {
    const data = await api.unbind(id);
    notify(`${data.detail} ${data.restored.map((r) => r.barcode).join("、")}`);
    await loadBindings();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}
</script>
