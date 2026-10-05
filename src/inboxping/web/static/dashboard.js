window.dashboardPage = () => ({
  overview: null,
  messages: [],
  total: 0,
  pageSize: 20,
  account: "",
  minRiskScore: "",
  pushOnly: false,
  offset: 0,
  loadingOverview: true,
  loadingMessages: true,
  messageRequestId: 0,
  selectedIds: [],
  bulkAction: null,
  activeMessageId: null,
  bulkProgress: "",
  bulkResult: "",
  bulkErrors: [],
  error: "",

  init() {
    const params = new URLSearchParams(window.location.search);
    this.account = params.get("account") || "";
    this.minRiskScore = params.get("min_risk_score") || "";
    this.pushOnly = params.get("push") === "true";
    const requestedSize = Number(params.get("limit"));
    if ([20, 50, 100].includes(requestedSize)) this.pageSize = requestedSize;
    const requestedOffset = Math.max(Number(params.get("offset")) || 0, 0);
    this.offset = Math.floor(requestedOffset / this.pageSize) * this.pageSize;
    this.refresh();
  },

  async refresh() {
    if (this.bulkAction !== null) return;
    this.error = "";
    await Promise.all([this.loadOverview(), this.loadMessages()]);
  },

  async loadOverview() {
    this.loadingOverview = true;
    try {
      this.overview = await InboxPing.apiFetch("/api/v1/overview");
    } catch (error) {
      this.error = error.message;
    } finally {
      this.loadingOverview = false;
    }
  },

  async loadMessages() {
    const requestId = ++this.messageRequestId;
    this.loadingMessages = true;
    const params = new URLSearchParams({ limit: this.pageSize, offset: this.offset });
    if (this.account) params.set("account", this.account);
    if (this.minRiskScore !== "") params.set("min_risk_score", this.minRiskScore);
    if (this.pushOnly) params.set("push", "true");
    try {
      const data = await InboxPing.apiFetch(`/api/v1/messages?${params}`);
      if (requestId !== this.messageRequestId) return;
      if (data.total && this.offset >= data.total) {
        this.offset = Math.floor((data.total - 1) / this.pageSize) * this.pageSize;
        this.syncUrl();
        return this.loadMessages();
      }
      this.messages = data.items;
      this.selectedIds = this.selectedIds.filter(id => data.items.some(mail => mail.id === id));
      this.total = data.total;
    } catch (error) {
      if (requestId === this.messageRequestId) this.error = error.message;
    } finally {
      if (requestId === this.messageRequestId) this.loadingMessages = false;
    }
  },

  applyFilters() {
    if (this.bulkAction !== null) return;
    this.resetSelection();
    this.offset = 0;
    this.syncUrl();
    this.loadMessages();
  },

  togglePush() {
    this.pushOnly = !this.pushOnly;
    this.applyFilters();
  },

  clearFilters() {
    this.account = "";
    this.minRiskScore = "";
    this.pushOnly = false;
    this.applyFilters();
  },

  changePage(delta) {
    this.goToPage(this.currentPage + delta);
  },

  goToPage(page) {
    if (this.bulkAction !== null) return;
    this.resetSelection();
    const target = Math.min(Math.max(page, 1), this.pageCount);
    this.offset = (target - 1) * this.pageSize;
    this.syncUrl();
    this.loadMessages();
  },

  changePageSize() {
    if (this.bulkAction !== null) return;
    this.resetSelection();
    this.offset = 0;
    this.syncUrl();
    this.loadMessages();
  },

  resetSelection() {
    this.selectedIds = [];
    this.bulkResult = "";
    this.bulkErrors = [];
  },

  toggleSelection(id, checked) {
    if (this.bulkAction !== null || this.loadingMessages) return;
    this.selectedIds = this.selectedIds.filter(selectedId => selectedId !== id);
    if (checked) this.selectedIds.push(id);
  },

  toggleAll(checked) {
    if (this.bulkAction !== null || this.loadingMessages) return;
    this.selectedIds = checked ? this.messages.map(mail => mail.id) : [];
  },

  async runBulkAction(action, selected = this.selectedMessages) {
    if (this.bulkAction !== null || this.loadingMessages) return;
    const messages = action === "notify"
      ? selected.filter(mail => mail.should_push === true)
      : selected;
    if (!messages.length) return;
    const label = action === "notify" ? "推送" : "分析";
    this.bulkAction = action;
    this.bulkResult = "";
    this.bulkErrors = [];
    this.error = "";
    let succeeded = 0;
    try {
      for (const [index, mail] of messages.entries()) {
        this.activeMessageId = mail.id;
        this.bulkProgress = `正在${label} ${index + 1}/${messages.length} 封`;
        try {
          const result = await InboxPing.apiFetch(`/api/v1/messages/${mail.id}/${action}`, {
            method: "POST",
          });
          if (result.status !== "completed") throw new Error("AI 分析失败，请查看邮件详情或重试");
          succeeded++;
        } catch (error) {
          this.bulkErrors.push({ id: mail.id, subject: mail.subject, message: error.message });
        }
      }
      const skipped = selected.length - messages.length;
      this.bulkResult = `${label}完成：成功 ${succeeded} 封，失败 ${this.bulkErrors.length} 封${skipped ? `，跳过 ${skipped} 封` : ""}。`;
      this.bulkProgress = "正在更新列表";
      await Promise.all([this.loadOverview(), this.loadMessages()]);
    } finally {
      this.bulkAction = null;
      this.activeMessageId = null;
      this.bulkProgress = "";
    }
  },

  syncUrl() {
    const params = new URLSearchParams();
    if (this.account) params.set("account", this.account);
    if (this.minRiskScore !== "") params.set("min_risk_score", this.minRiskScore);
    if (this.pushOnly) params.set("push", "true");
    if (this.pageSize !== 20) params.set("limit", this.pageSize);
    if (this.offset) params.set("offset", this.offset);
    history.replaceState(null, "", `${location.pathname}${params.size ? `?${params}` : ""}`);
  },

  formatDate: InboxPing.formatDate,
  get selectedMessages() { return this.messages.filter(mail => this.selectedIds.includes(mail.id)); },
  get selectedPushCount() { return this.selectedMessages.filter(mail => mail.should_push === true).length; },
  get allSelected() { return this.messages.length > 0 && this.selectedMessages.length === this.messages.length; },
  get partiallySelected() { return this.selectedMessages.length > 0 && !this.allSelected; },
  get currentPage() { return Math.floor(this.offset / this.pageSize) + 1; },
  get pageCount() { return Math.max(1, Math.ceil(this.total / this.pageSize)); },
  get pageStart() { return this.total ? this.offset + 1 : 0; },
  get pageEnd() { return Math.min(this.offset + this.pageSize, this.total); },
  get visiblePages() {
    const start = Math.max(1, Math.min(this.currentPage - 2, this.pageCount - 4));
    const end = Math.min(this.pageCount, start + 4);
    return Array.from({ length: end - start + 1 }, (_, index) => start + index);
  },
  accountStatus(status) { return InboxPing.accountStatusLabels[status] || status; },
});
