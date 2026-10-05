const messageMarkdown = new marked.Marked({
  gfm: true,
  breaks: true,
  tokenizer: {
    url(src) {
      // Bare URLs in Chinese text end at sentence punctuation.
      const boundary = src.search(/[，。；：！？、“”‘’（）【】《》]/u);
      return marked.Tokenizer.prototype.url.call(this, boundary < 0 ? src : src.slice(0, boundary));
    },
  },
  renderer: {
    html({ text }) {
      const element = document.createElement("div");
      element.textContent = text;
      return element.innerHTML;
    },
  },
});

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
  notice: "",
  error: "",

  init() { this.load(); },

  renderMarkdown(text) {
    return DOMPurify.sanitize(messageMarkdown.parse(text || ""), {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["img"],
    });
  },

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
    this.notice = "";
    try {
      await InboxPing.apiFetch(`/api/v1/messages/${this.messageId}/${action}`, {
        method: "POST",
      });
      await this.load(false);
      if (action === "refresh-body" && !this.error) {
        this.notice = this.message.translation
          ? "正文已更新，已有译文保留。可点击“重新翻译”更新译文。"
          : "正文已重新读取并更新。";
      }
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
          this.notice = "";
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
    this.notice = "";
  },

  formatDate: InboxPing.formatDate,
  deliveryLabel(status) { return InboxPing.deliveryLabels[status] || status; },
});
