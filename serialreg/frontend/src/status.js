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

// 实际可得性（定位/时间轴联动显示）
export const AVAILABILITY = {
  available: { label: "可借", cls: "ok" },
  on_loan: { label: "借出中", cls: "loaned" },
  bound: { label: "已装订", cls: "combined" },
  lost: { label: "丢失", cls: "missing" },
};

export const LOAN_EVENT = {
  checkout: "借出",
  return: "归还",
  overdue: "逾期",
  lost: "遗失",
};

export const LOAN_STATUS = {
  open: "在借",
  closed: "已关闭",
};

export const ISSUE_KIND = { regular: "普通期", combined: "两期合刊" };
