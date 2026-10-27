/* Read-only enhancement: ordinary task links remain usable without JavaScript. */
(() => {
  "use strict";
  const shell = document.querySelector("#task-panel-dialog");
  if (!shell || typeof window.HTMLDialogElement?.prototype.showModal !== "function") return;
  const initialUser = document.querySelector("[data-board-root]")?.dataset.userId;
  if (!initialUser) return;
  const dialog = document.createElement("dialog");
  dialog.id = shell.id; dialog.className = shell.className;
  dialog.setAttribute("aria-labelledby", shell.getAttribute("aria-labelledby"));
  dialog.append(...shell.childNodes); shell.replaceWith(dialog);
  const content = document.querySelector("#task-panel-content");
  const status = document.querySelector("#task-panel-status");
  const recovery = document.querySelector("#task-panel-recovery");
  const fallback = document.querySelector("#task-panel-fallback");
  const retry = document.querySelector("#task-panel-retry");
  const closeButton = document.querySelector("#task-panel-close");
  const previous = document.querySelector("#task-panel-prev");
  const next = document.querySelector("#task-panel-next");
  const body = dialog.querySelector(".task-panel-body");
  const links = () => Array.from(document.querySelectorAll("[data-board-root] .board-card h4 a"));
  let controller, sequence = 0, current, returnTo, scroll, styles, locked = false;
  function navigation() {
    const items = links(), index = items.findIndex(a => a.href === current?.href);
    previous.disabled = index <= 0;
    next.disabled = index < 0 || index >= items.length - 1;
    if (document.activeElement === next && next.disabled) (previous.disabled ? closeButton : previous).focus({preventScroll:true});
    if (document.activeElement === previous && previous.disabled) (next.disabled ? closeButton : next).focus({preventScroll:true});
  }
  function clear() {
    content.replaceChildren(); recovery.hidden = true;
    content.removeAttribute("aria-busy");
  }
  function failed(message, canRetry) {
    clear(); status.textContent = message;
    recovery.hidden = false; retry.hidden = !canRetry;
  }
  function accessChanged(message) {
    failed(message, false);
    document.dispatchEvent(new CustomEvent("task-panel:revoked", {detail:{message}}));
  }
  async function load(link) {
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !/\/tasks\/\d+$/.test(url.pathname)) return;
    if (document.querySelector("#board-dialog[open]")) return;
    controller?.abort(); controller = new AbortController();
    const request = ++sequence, signal = controller.signal;
    current = url;
    if (!dialog.open) {
      returnTo = link;
      scroll = {left: window.scrollX, top: window.scrollY};
      styles = {overflow: document.body.style.overflow, padding: document.body.style.paddingRight};
      const gap = window.innerWidth - document.documentElement.clientWidth;
      const padding = parseFloat(getComputedStyle(document.body).paddingRight) || 0;
      document.body.style.paddingRight = `${padding + gap}px`;
      document.body.style.overflow = "hidden";
      locked = true;
      dialog.showModal(); closeButton.focus({preventScroll: true});
    }
    clear(); body.scrollTop = 0; status.textContent = "Загружаем задачу…";
    content.setAttribute("aria-busy", "true"); fallback.href = url.href; navigation();
    try {
      const response = await fetch(url.pathname + "/panel", {signal, credentials: "same-origin",
        cache: "no-store", redirect: "manual", headers: {Accept: "text/html"}});
      if (request !== sequence || !dialog.open) return;
      if (response.type === "opaqueredirect" || response.status === 401) {
        accessChanged("Сеанс завершён. Откройте страницу заново и войдите снова."); return;
      }
      if (response.status === 403 || response.status === 404) {
        accessChanged(response.status === 403 ? "Доступ к задаче закрыт. Откройте страницу заново." : "Задача не найдена или недоступна. Откройте страницу заново."); return;
      }
      const id = url.pathname.split("/").pop();
      if (!response.ok || response.redirected || response.headers.get("X-Task-Panel") !== id ||
          !response.headers.get("Content-Type")?.includes("text/html")) throw new Error("Invalid panel response");
      if (response.headers.get("X-Task-User") !== initialUser) {
        accessChanged("Учётная запись изменилась или не подтверждена. Откройте страницу заново."); return;
      }
      const html = await response.text();
      if (request !== sequence || !dialog.open) return;
      const parsed = new DOMParser().parseFromString(html, "text/html");
      const article = parsed.querySelector("[data-task-panel-content]");
      if (!article || article.dataset.taskId !== id || parsed.querySelector("script, iframe, form"))
        throw new Error("Invalid panel content");
      content.replaceChildren(document.importNode(article, true));
      content.removeAttribute("aria-busy"); status.textContent = "";
    } catch (error) {
      if (signal.aborted || request !== sequence || !dialog.open) return;
      failed("Не удалось загрузить задачу. Проверьте соединение и повторите попытку.", true);
    }
  }
  function finishClose() {
    if (!locked) return;
    locked = false;
    ++sequence; controller?.abort(); clear(); status.textContent = "";
    document.body.style.overflow = styles?.overflow || "";
    document.body.style.paddingRight = styles?.padding || "";
    if (scroll) window.scrollTo(scroll);
    const visible = node => { const r = node?.getBoundingClientRect(); return r && r.top >= 80 && r.top < innerHeight - 44; };
    const target = returnTo?.isConnected && visible(returnTo) ? returnTo : links().find(visible) || document.querySelector(".task-board");
    target?.focus({preventScroll: true});
  }
  function close() { dialog.close(); finishClose(); }
  dialog.addEventListener("close", () => {
    // An old queued close event must not clear a newly opened preview.
    if (!dialog.open) finishClose();
  });
  closeButton.addEventListener("click", close);
  dialog.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const items = Array.from(dialog.querySelectorAll('button:not([disabled]),a[href],summary,[tabindex="0"]'))
      .filter(item => item.getClientRects().length && !item.closest("[hidden]"));
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first?.focus();
    }
  });
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  retry.addEventListener("click", () => load(current));
  previous.addEventListener("click", () => {
    const items = links(), i = items.findIndex(a => a.href === current?.href);
    if (i > 0) load(items[i - 1]);
  });
  next.addEventListener("click", () => {
    const items = links(), i = items.findIndex(a => a.href === current?.href);
    if (i >= 0 && i + 1 < items.length) load(items[i + 1]);
  });
  document.addEventListener("task-board:revoked", () => {
    if (dialog.open) close();
    ++sequence; controller?.abort(); clear(); current = null;
  });
  document.addEventListener("task-board:action-closed", event => {
    const link = links().find(a => a.href === new URL(event.detail.href,location.href).href);
    load(link || {href:event.detail.href});
  });
  document.addEventListener("click", event => {
    const action = event.target.closest("[data-panel-action]");
    if (action && !event.defaultPrevented && !event.button && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      const accepted = !document.dispatchEvent(new CustomEvent("task-board:action",{cancelable:true,
        detail:{id:Number(action.dataset.taskId),href:new URL(action.href).pathname}}));
      if (accepted) { event.preventDefault(); close(); }
      return;
    }
    const link = event.target.closest("[data-board-root] .board-card h4 a");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey ||
        event.shiftKey || event.altKey || link.target || link.hasAttribute("download")) return;
    if (document.querySelector("#board-dialog[open]")) return;
    event.preventDefault(); load(link);
  });
})();
