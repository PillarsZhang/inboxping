window.messagePage = (messageId) => ({
  messageId,
  message: null,
  recipientsExpanded: false,
  recipientList: [],
  copiedRecipients: false,
  loading: true,
  action: null,
  translating: false,
  translationDraft: "",
  translationStatus: "",
  translationError: "",
  error: "",

  init() { this.load(); },

  async copyRecipients() {
    try {
      await navigator.clipboard.writeText(this.recipientList.join("\n"));
      this.copiedRecipients = true;
    } catch (_) {
      this.error = "复制失败，请手动选择收件地址";
    }
  },

  async load(showLoading = true) {
    if (showLoading) this.loading = true;
    this.error = "";
    try {
      this.message = await InboxPing.apiFetch(`/api/v1/messages/${this.messageId}`);
      this.recipientList = this.message.recipient_addresses || [];
      this.copiedRecipients = false;
      document.title = `${this.message.analysis?.title || this.message.subject} · InboxPing`;
    } catch (error) {
      this.error = error.message;
    } finally {
      if (showLoading) this.loading = false;
    }
  },

  async runAction(action) {
    this.action = action;
    this.error = "";
    try {
      await InboxPing.apiFetch(`/api/v1/messages/${this.messageId}/${action}`, {
        method: "POST",
      });
      await this.load(false);
    } catch (error) {
      this.error = error.message;
    } finally {
      this.action = null;
    }
  },

  async runTranslation() {
    this.translating = true;
    this.error = "";
    this.translationDraft = "";
    this.translationStatus = "正在连接 AI";
    this.translationError = "";
    try {
      const force = this.message?.translation ? "?force=true" : "";
      let completed = false;
      for await (const event of InboxPing.streamNdjson(
        `/api/v1/messages/${this.messageId}/translate${force}`,
        { method: "POST" },
      )) {
        if (event.type === "start") this.translationStatus = "等待模型输出";
        if (event.type === "delta") {
          this.translationDraft += event.text;
          this.translationStatus = `正在翻译，已生成 ${this.translationDraft.length} 字`;
        }
        if (event.type === "error") throw new Error(event.message || "翻译失败");
        if (event.type === "complete") {
          completed = true;
          this.translationStatus = "翻译完成";
          this.message.translation = event.translation;
          this.translationDraft = "";
        }
      }
      if (!completed) throw new Error("翻译连接意外中断");
    } catch (error) {
      if (this.message?.translation) this.translationDraft = "";
      this.translationError = error.message;
      this.translationStatus = "翻译未完成";
    } finally {
      this.translating = false;
    }
  },

  async deleteTranslation() {
    const hasSavedTranslation = Boolean(this.message?.translation);
    if (hasSavedTranslation && !window.confirm("确定删除本地保存的译文吗？原邮件不会受到影响。")) {
      return;
    }
    this.translationError = "";
    if (hasSavedTranslation) {
      try {
        await InboxPing.apiFetch(`/api/v1/messages/${this.messageId}/translation`, {
          method: "DELETE",
        });
      } catch (error) {
        this.translationError = error.message;
        return;
      }
    }
    this.message.translation = null;
    this.translationDraft = "";
    this.translationStatus = "";
    this.translationError = "";
  },

  formatDate: InboxPing.formatDate,
  deliveryLabel(status) { return InboxPing.deliveryLabels[status] || status; },
});
