window.InboxPing = (() => {
  async function apiFetch(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (response.status === 401) {
      const next = encodeURIComponent(`${window.location.pathname}${window.location.search}`);
      window.location.assign(`/login?next=${next}`);
      throw new Error("登录状态已失效");
    }
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        const body = await response.json();
        if (body.detail) message = body.detail;
      } catch (_) {
        // Keep the HTTP status message when the response is not JSON.
      }
      throw new Error(message);
    }
    return response.json();
  }

  async function* streamNdjson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/x-ndjson", ...(options.headers || {}) },
      ...options,
    });
    if (response.status === 401) {
      const next = encodeURIComponent(`${window.location.pathname}${window.location.search}`);
      window.location.assign(`/login?next=${next}`);
      throw new Error("登录状态已失效");
    }
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        const body = await response.json();
        if (body.detail) message = body.detail;
      } catch (_) {
        // Keep the HTTP status message when the response is not JSON.
      }
      throw new Error(message);
    }
    if (!response.body) throw new Error("浏览器不支持流式响应");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.trim()) yield JSON.parse(line);
      }
      if (done) break;
    }
    if (buffer.trim()) yield JSON.parse(buffer);
  }

  function formatDate(value, withSeconds = false) {
    if (!value) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: withSeconds ? "2-digit" : undefined,
      hour12: false,
    }).format(new Date(value)).replaceAll("/", "-");
  }

  const riskLabels = {
    high: "高风险",
    medium: "中风险",
    low: "低风险",
    unknown: "风险未知",
  };
  const deliveryLabels = { sent: "已推送", failed: "失败", pending: "待发送" };
  const accountStatusLabels = {
    connected: "已连接",
    starting: "连接中",
    error: "异常",
    disabled: "已停用",
  };

  return { apiFetch, streamNdjson, formatDate, riskLabels, deliveryLabels, accountStatusLabels };
})();
