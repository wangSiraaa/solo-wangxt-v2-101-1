// 馆藏/编号槽位状态的展示字典。
// not_published / ceased_gap 都是「缺号」——没有发行记录，不自动等同缺藏。
export const HOLDING_STATUS = {
  "issued+held": { label: "已入藏", cls: "ok", hint: "已发行且有在馆实物" },
  "issued+missing": {
    label: "缺藏",
    cls: "missing",
    hint: "已发行但没有可用实物",
  },
  not_published: {
    label: "缺号",
    cls: "gap",
    hint: "没有发行记录，不自动等同缺藏",
  },
  ceased_gap: {
    label: "停刊后缺号",
    cls: "ceased",
    hint: "停刊月份之后，再无发行",
  },
  unregistered: { label: "未登记", cls: "gap", hint: "编号槽位不存在" },
};

export const ITEM_STATUS = {
  available: "在馆",
  checked_out: "借出",
  lost: "丢失",
  bound: "已装订",
};

// 实际可得性（流通派生视图，比 item.status 更细：逾期不入库，读时派生）
export const AVAILABILITY = {
  available: { label: "可借", cls: "ok", hint: "在馆，可借出" },
  checked_out: { label: "借出中", cls: "out", hint: "在读者手中，到期日前归还" },
  overdue: { label: "逾期", cls: "missing", hint: "已超过到期日" },
  lost: { label: "遗失", cls: "missing", hint: "实物遗失，缺藏" },
  bound: { label: "已装订", cls: "bound", hint: "装订册整体保管，不能单件外借" },
};

export const LOAN_STATUS = {
  checked_out: "借出中",
  returned: "已归还",
  lost: "遗失",
};

export const fmtDT = (s) => (s ? s.replace("T", " ").slice(0, 16) : "—");
export const fmtDay = (s) => (s ? s.slice(0, 10) : "—");

export const ISSUE_KIND = { regular: "普通期", combined: "两期合刊" };
