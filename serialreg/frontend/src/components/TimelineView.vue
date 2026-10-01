<template>
  <div class="panel">
    <h2>
      期号时间轴
      <span class="muted">
        —— {{ data.title.title }}
        <span v-if="data.title.status === 'ceased'" class="badge ceased">
          停刊于 {{ data.title.ceased_month?.slice(0, 7) }}
        </span>
      </span>
    </h2>

    <div class="timeline">
      <div
        v-for="slot in data.slots"
        :key="slot.number_id"
        class="slot"
        :class="dotClass(slot.holding_status)"
      >
        <span class="dot"></span>
        <div class="slot-card" :class="{ gap: isGap(slot.holding_status) }">
          <div class="slot-head">
            <span class="slot-no">
              v.{{ slot.volume || "—" }} no.{{ slot.number }}
            </span>
            <span class="badge" :class="badgeCls(slot.holding_status)">
              {{ statusMeta(slot.holding_status).label }}
            </span>
            <span class="slot-hint">{{ statusMeta(slot.holding_status).hint }}</span>
          </div>

          <!-- 缺号：没有发行记录，不展示入藏入口暗示 -->
          <template v-if="slot.issues.length === 0">
            <p class="empty-hint">
              该编号槽位没有发行记录（缺号）。只有登记了发行期，才能为其入藏。
            </p>
          </template>

          <div
            v-for="iss in slot.issues"
            :key="iss.issue_id"
            class="issue-box"
            :class="{ combined: iss.kind === 'combined' }"
          >
            <div class="slot-head">
              <span class="issue-month">{{ monthRange(iss) }}</span>
              <span v-if="iss.kind === 'combined'" class="badge combined">
                合刊 {{ iss.combined_numbers.map(n => `v.${n.volume||"—"}no.${n.number}`).join(" + ") }}
              </span>
              <span v-else class="muted">普通期</span>
            </div>

            <p v-if="iss.items.length === 0" class="empty-hint">
              已发行但尚无实物 —— 此为「缺藏」，请在右侧入藏面板登记。
            </p>

            <div v-for="it in iss.items" :key="it.barcode" class="item-line">
              <code>{{ it.barcode }}</code>
              <span class="badge" :class="it.status === 'lost' ? 'missing' : 'ok'">
                {{ itemStatus[it.status] || it.status }}
              </span>
              <span class="loc">
                📍 {{ it.location || "（未排架）" }}
                <template v-if="it.bound">（装订册 {{ it.binding }}）</template>
              </span>
              <button
                v-if="!it.bound && it.status !== 'lost'"
                class="tiny ghost"
                @click="$emit('mark-lost', it.item_id)"
                title="标记丢失后，该期变为缺藏"
              >报失</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { HOLDING_STATUS, ITEM_STATUS } from "../status.js";

defineProps({ data: Object });
defineEmits(["mark-lost"]);
const itemStatus = ITEM_STATUS;

const isGap = (s) => s === "not_published" || s === "ceased_gap";

function statusMeta(s) {
  return HOLDING_STATUS[s] || { label: s, cls: "gap", hint: "" };
}
function badgeCls(s) {
  return statusMeta(s).cls;
}
function dotClass(s) {
  if (s === "issued+held") return "held";
  if (s === "issued+missing") return "missing";
  return "gap";
}
function monthRange(iss) {
  const a = iss.issue_month?.slice(0, 7);
  const b = iss.issue_month_end?.slice(0, 7);
  return b && b !== a ? `${a} ~ ${b}` : a;
}
</script>
