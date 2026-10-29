/* Read-only task viewer. The server remains authoritative for identity and actions. */
(() => {
  "use strict";
  const shell = document.querySelector("#task-panel-dialog");
  const root = document.querySelector("[data-board-root]");
  if (!shell || !root?.dataset.userId || typeof HTMLDialogElement === "undefined" || typeof HTMLDialogElement.prototype.showModal !== "function") return;
  const initialUser = root.dataset.userId;
  const dialog = document.createElement("dialog");
  dialog.id = shell.id;
  dialog.className = shell.className;
  dialog.setAttribute("aria-labelledby", shell.getAttribute("aria-labelledby"));
  dialog.append(...shell.childNodes);
  shell.replaceWith(dialog);
  const content = dialog.querySelector("#task-panel-content");
  const status = dialog.querySelector("#task-panel-status");
  const recovery = dialog.querySelector("#task-panel-recovery");
  const fallback = dialog.querySelector("#task-panel-fallback");
  const reload = dialog.querySelector("#task-panel-reload");
  const retry = dialog.querySelector("#task-panel-retry");
  const closeButton = dialog.querySelector("#task-panel-close");
  const previous = dialog.querySelector("#task-panel-prev");
  const next = dialog.querySelector("#task-panel-next");
  const body = dialog.querySelector(".task-panel-body");
  const links = () => [...document.querySelectorAll("[data-board-root] .board-card h4 a")];
  let controller, sequence = 0, current, opener, position, styles, locked = false, revoked = false;
  function taskURL(href) {
    try {
      const url = new URL(href, location.href);
      if (url.origin !== location.origin || url.username || url.password || !/^\/tasks\/\d+$/.test(url.pathname)) return null;
      url.hash = "";
      return url;
    } catch { return null; }
  }
  function clear() {
    content.replaceChildren();
    content.removeAttribute("aria-busy");
    recovery.hidden = true;
  }
  function announce(text, state) {
    status.textContent = text;
    status.dataset.state = state;
    status.setAttribute("role", state === "error" ? "alert" : "status");
    status.classList.toggle("sr-only", state === "ready");
  }
  function navigation() {
    const items = links(), index = items.findIndex(a => taskURL(a.href)?.href === current?.href);
    previous.disabled = revoked || index <= 0;
    next.disabled = revoked || index < 0 || index >= items.length - 1;
    if (document.activeElement === next && next.disabled) (previous.disabled ? closeButton : previous).focus({preventScroll:true});
    if (document.activeElement === previous && previous.disabled) (next.disabled ? closeButton : next).focus({preventScroll:true});
  }
  function failed(message, canRetry = true) {
    clear();
    announce(message, "error");
    recovery.hidden = false;
    retry.hidden = !canRetry;
    fallback.hidden = !canRetry;
    reload.hidden = canRetry;
  }
  function revoke(message, dispatch = true) {
    if (revoked) return;
    revoked = true;
    ++sequence;
    controller?.abort();
    failed(message, false);
    fallback.removeAttribute("href");
    navigation();
    const board = document.querySelector("[data-board-root]");
    board?.replaceChildren();
    if (board) for (const name of ["data-counts","data-total","data-csrf-token","data-user-id","data-refresh-url"]) board.removeAttribute(name);
    document.querySelectorAll(".task-tab-count").forEach(node => { node.textContent = "—"; node.removeAttribute("aria-label"); });
    document.querySelectorAll(".task-list-heading,.department-context").forEach(node => node.replaceChildren());
    document.querySelectorAll("[data-task-view][data-user-id]").forEach(node => node.removeAttribute("data-user-id"));
    const feedback = document.querySelector("#board-feedback");
    if (feedback) { feedback.hidden = false; feedback.textContent = message; feedback.tabIndex = -1; }
    if (dispatch) document.dispatchEvent(new CustomEvent("task-panel:revoked", {detail:{message}}));
    status.focus({preventScroll:true});
  }
  function validArticle(html, id) {
    const parsed = new DOMParser().parseFromString(html, "text/html");
    if (parsed.querySelector("script,iframe,form,input,textarea,select,button,object,embed,link,style,base,meta,img,image,video,audio")) return null;
    const articles = parsed.querySelectorAll("[data-task-panel-content]");
    const article = articles.length === 1 && articles[0];
    if (!article || article.dataset.taskId !== id) return null;
    for (const node of [article, ...article.querySelectorAll("*")]) {
      for (const attr of node.attributes) {
        const name = attr.name.toLowerCase();
        if (name.startsWith("on") || name === "srcdoc" || name === "style") return null;
        if (["href","src","xlink:href"].includes(name)) {
          try {
            const url = new URL(attr.value, location.href);
            if (!["http:","https:"].includes(url.protocol) || url.origin !== location.origin || url.username || url.password) return null;
          } catch { return null; }
        }
      }
    }
    return article;
  }
  async function load(link) {
    const url = taskURL(link?.href);
    if (!url || revoked || document.querySelector("#board-dialog[open]")) return;
    const retryHadFocus = document.activeElement === retry;
    controller?.abort();
    controller = new AbortController();
    const request = ++sequence, signal = controller.signal;
    current = url;
    if (!dialog.open) {
      opener = link;
      position = {left:scrollX,top:scrollY};
      styles = {overflow:document.body.style.overflow,padding:document.body.style.paddingRight};
      const gap = innerWidth - document.documentElement.clientWidth;
      document.body.style.paddingRight = (parseFloat(getComputedStyle(document.body).paddingRight) || 0) + gap + "px";
      document.body.style.overflow = "hidden";
      locked = true;
      dialog.showModal();
      closeButton.focus({preventScroll:true});
    }
    clear();
    body.scrollTop = 0;
    announce("Загружаем задачу…", "loading");
    content.setAttribute("aria-busy", "true");
    fallback.href = url.href;
    navigation();
    const endpoint = new URL(url.href);
    endpoint.pathname += "/panel"; // Preserve the exact server-provided query context.
    try {
      const response = await fetch(endpoint, {signal,credentials:"same-origin",cache:"no-store",redirect:"manual",headers:{Accept:"text/html"}});
      if (signal.aborted || request !== sequence || !dialog.open || revoked) return;
      if (response.type === "opaqueredirect" || response.status === 401) {
        revoke("Сеанс завершён. Откройте страницу заново и войдите снова."); return;
      }
      if (response.status === 403 || response.status === 404) {
        revoke(response.status === 403 ? "Доступ к задаче закрыт. Откройте страницу заново." : "Задача не найдена или недоступна. Откройте страницу заново."); return;
      }
      const id = url.pathname.split("/").pop();
      if (!response.ok || response.redirected || response.headers.get("X-Task-Panel") !== id ||
          !response.headers.get("Content-Type")?.includes("text/html")) throw new Error("Invalid task response");
      if (response.headers.get("X-Task-User") !== initialUser) {
        revoke("Учётная запись изменилась или не подтверждена. Откройте страницу заново."); return;
      }
      const html = await response.text();
      if (signal.aborted || request !== sequence || !dialog.open || revoked) return;
      const article = validArticle(html, id);
      if (!article) throw new Error("Invalid read-only content");
      content.replaceChildren(document.importNode(article, true));
      content.removeAttribute("aria-busy");
      announce("Задача загружена.", "ready");
      if (retryHadFocus) {
        const title = content.querySelector(".task-panel-title");
        title?.setAttribute("tabindex", "-1");
        (title || closeButton).focus({preventScroll:true});
      }
    } catch {
      if (signal.aborted || request !== sequence || !dialog.open || revoked) return;
      failed("Не удалось загрузить задачу. Повторите загрузку или откройте полную страницу.");
    }
  }
  function finishClose(restore = true) {
    if (!locked) return;
    locked = false;
    ++sequence;
    controller?.abort();
    clear();
    announce("", "idle");
    document.body.style.overflow = styles?.overflow || "";
    document.body.style.paddingRight = styles?.padding || "";
    if (restore) {
      const target = !revoked && opener?.isConnected ? opener : document.querySelector(revoked ? "#board-feedback" : ".task-board");
      target?.focus({preventScroll:true});
      if (position) window.scrollTo(position);
    }
  }
  function close(restore = true) { dialog.close(); finishClose(restore); }
  dialog.addEventListener("close", () => { if (!dialog.open) finishClose(); });
  closeButton.addEventListener("click", () => close());
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  dialog.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const items = [...dialog.querySelectorAll('button:not([disabled]),a[href],summary,[tabindex="0"]')]
      .filter(node => node.getClientRects().length && !node.closest("[hidden]") && getComputedStyle(node).visibility !== "hidden");
    if (event.shiftKey && document.activeElement === items[0]) {
      event.preventDefault(); items.at(-1)?.focus();
    } else if (!event.shiftKey && document.activeElement === items.at(-1)) {
      event.preventDefault(); items[0]?.focus();
    }
  });
  retry.addEventListener("click", () => load(current));
  previous.addEventListener("click", () => {
    const items = links(), index = items.findIndex(a => taskURL(a.href)?.href === current?.href);
    if (index > 0) load(items[index - 1]);
  });
  next.addEventListener("click", () => {
    const items = links(), index = items.findIndex(a => taskURL(a.href)?.href === current?.href);
    if (index >= 0 && index + 1 < items.length) load(items[index + 1]);
  });
  document.addEventListener("task-board:revoked", () => {
    if (!revoked) revoke("Доступ изменился. Откройте страницу заново.", false);
  });
  document.addEventListener("task-board:action-closed", event => {
    const href = taskURL(event.detail?.href)?.href;
    if (href) load(links().find(a => taskURL(a.href)?.href === href) || {href});
  });
  document.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const action = event.target.closest("[data-panel-action]");
    if (action) {
      const url = taskURL(action.href);
      if (!url) return;
      const accepted = !document.dispatchEvent(new CustomEvent("task-board:action", {cancelable:true,
        detail:{id:Number(action.dataset.taskId),href:url.href}}));
      if (accepted) { event.preventDefault(); close(); }
      return;
    }
    const link = event.target.closest("[data-board-root] .board-card h4 a,[data-board-root] .task-open");
    if (!link || link.target || link.hasAttribute("download") || !taskURL(link.href) || revoked || document.querySelector("#board-dialog[open]")) return;
    event.preventDefault();
    load(link);
  });
  window.addEventListener("pagehide", () => { if (locked) close(false); });
})();
