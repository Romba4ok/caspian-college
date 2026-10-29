/* Board UI only: the existing workflow endpoints remain authoritative. */
(() => {
  "use strict";
  const viewSwitch = document.querySelector("[data-task-view]");
  const fullMode = !!document.querySelector("[data-full-action-root]");
  if (!viewSwitch && !fullMode) return;
  // Plain task URLs always use the server default (board); ?view=list is explicit.
  const dialog = document.querySelector("#board-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  if (dialog.dataset.actionBound) return;
  dialog.dataset.actionBound = "true";
  if (fullMode) document.documentElement.classList.add("actions-enhanced");
  const feedback = document.querySelector("#board-feedback");
  feedback.tabIndex = -1;
  const form = document.querySelector("#board-action-form");
  const actionSelect = document.querySelector("#board-action");
  const assignee = document.querySelector("#board-assignee");
  const deadline = document.querySelector("#board-deadline");
  const comment = document.querySelector("#board-comment");
  const submit = document.querySelector("#board-submit");
  const cancel = document.querySelector("#board-cancel");
  const error = document.querySelector("#board-error");
  const refresh = document.querySelector(".board-refresh");
  const saving = document.querySelector("#board-saving");
  let columns = [];
  const columnStates = new Map();
  const columnActions = (data, column) => !data || data.group === column.dataset.group ? [] :
    data.actions.filter(action => !action.terminal && columnStates.get(column).includes(action.target));
  let active = null, source = null, opener = null, pending = false, blocked = false;
  let pointerDrag = null, phase = "editing", fromPanel = false, savedPosition;
  let needsRead = false, revoked = false, bodyStyle = null;
  const rootSelector = fullMode ? "[data-full-action-root]" : "[data-board-root]";
  const initialUser = document.querySelector(rootSelector).dataset.userId;
  const initialDirection = viewSwitch?.dataset.direction || "detail";
  const readURL = document.querySelector(rootSelector).dataset.refreshUrl;
  const findCard = id => document.querySelector('[data-board-task][data-task-id="'+Number(id)+'"]');
  const hints = JSON.parse(dialog.dataset.actionHints || "{}");
  const retryBoard = document.querySelector("#board-retry");
  const discard = document.querySelector("#board-discard");
  const say = message => { feedback.textContent = message; feedback.hidden = false; };
  function purge(message) {
    revoked = true; needsRead = true; states.clear();
    const root = document.querySelector(rootSelector);
    root?.replaceChildren();
    if (root) for (const name of ["data-counts","data-total","data-csrf-token","data-user-id","data-refresh-url"]) root.removeAttribute(name);
    document.querySelectorAll(".task-list-heading .hint,.task-page-heading .hint").forEach(e => { e.textContent = ""; });
    active = null; source = null; pointerDrag = null; columns = []; columnStates.clear();
    document.querySelectorAll(".task-tab-count").forEach(e => { e.textContent = "—"; e.removeAttribute("aria-label"); });
    document.querySelectorAll(".task-list-heading,.department-context").forEach(e => e.replaceChildren());
    if (fullMode) document.title="Задача недоступна · Caspian College";
    document.dispatchEvent(new CustomEvent("task-board:revoked"));
    form.reset(); actionSelect.replaceChildren(); assignee.replaceChildren();
    document.dispatchEvent(new CustomEvent("ui:validate-files",{detail:{form}}));
    form.querySelector('[name="csrf_token"]').value = "";
    document.querySelector("#board-dialog-task").textContent = "Задача недоступна";
    say(message); retryBoard.hidden = true;
    refresh.hidden=false; refresh.href=location.href; refresh.textContent="Открыть страницу заново";
  }
  document.addEventListener("task-panel:revoked", event => {
    purge(event.detail.message);
    feedback.focus({preventScroll:true});
  });
  async function readBoard() {
    const url = new URL(readURL, location.href);
    if (url.origin !== location.origin) throw new Error("Invalid origin");
    const response = await fetch(url, {credentials:"same-origin", cache:"no-store", redirect:"manual", headers:{Accept:"text/html","X-Task-Read":"1"}});
    if (response.type === "opaqueredirect" || [401,403,404].includes(response.status)) {
      purge("Доступ или сеанс изменился. Откройте страницу заново и войдите при необходимости.");
      throw new Error("Access changed");
    }
    if (fullMode) {
      if (response.headers.has("X-Task-User") && response.headers.get("X-Task-User")!==initialUser) {
        purge("Учётная запись изменилась. Откройте страницу заново."); throw new Error("Identity changed");
      }
      if (!response.ok || !response.headers.get("Content-Type")?.includes("text/html")) throw new Error("Invalid task response");
      const parsed = new DOMParser().parseFromString(await response.text(),"text/html");
      const fresh = parsed.querySelector("[data-full-action-root]");
      if (!fresh || fresh.dataset.userId !== initialUser) {
        purge("Учётная запись или доступ к задаче изменились. Откройте страницу заново."); throw new Error("Identity changed");
      }
      if (fresh.dataset.taskId !== document.querySelector(rootSelector).dataset.taskId ||
          fresh.querySelector("script,iframe,object,embed,base,style")) throw new Error("Invalid task fragment");
      validateNodes(fresh);
      if (!fresh.dataset.csrfToken) throw new Error("Missing form token");
      form.querySelector('[name="csrf_token"]').value=fresh.dataset.csrfToken;
      document.querySelector(rootSelector).replaceWith(document.importNode(fresh,true));
      bindBoard(); needsRead = false; retryBoard.hidden = true; return;
    }
    if (!response.ok || response.headers.get("X-Task-Board") !== initialDirection ||
        !response.headers.get("Content-Type")?.includes("text/html")) throw new Error("Invalid board response");
    if (response.headers.get("X-Task-User") !== initialUser) {
      purge("Учётная запись изменилась. Откройте страницу заново."); throw new Error("User changed");
    }
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    const fresh = parsed.querySelector("[data-board-root]");
    if (!fresh || fresh.dataset.userId !== initialUser || fresh.dataset.direction !== initialDirection || parsed.querySelector("script,iframe,form"))
      throw new Error("Invalid board fragment");
    validateNodes(fresh);
    const counts = JSON.parse(fresh.dataset.counts);
    if (!["received","sent","completed"].every(k => Number.isInteger(counts[k]) && counts[k] >= 0)) throw new Error("Invalid totals");
    const visibleTotal = Number(fresh.dataset.total ?? counts[initialDirection]);
    if (!Number.isInteger(visibleTotal) || visibleTotal < 0) throw new Error("Invalid visible total");
    const position = {left:window.scrollX,top:window.scrollY};
    document.querySelector("[data-board-root]").replaceWith(document.importNode(fresh,true));
    form.querySelector('[name="csrf_token"]').value = fresh.dataset.csrfToken;
    document.querySelectorAll(".task-tabs a").forEach(link => {
      const name = new URL(link.href).pathname.split("/").pop(), badge = link.querySelector(".task-tab-count");
      if (badge && Object.hasOwn(counts,name)) { badge.textContent = counts[name]; badge.setAttribute("aria-label",`Всего задач: ${counts[name]}`); }
    });
    const total = document.querySelector(".task-list-heading .hint"); if (total) total.textContent = `Всего: ${visibleTotal}`;
    bindBoard();
    if (phase === "posted" && active) document.querySelector(`[data-task-id="${active.id}"]`)?.classList.add("board-card-updated");
    window.scrollTo(position); needsRead = false; retryBoard.hidden = true;
  }
  function canonical(href) {
    try { const url=new URL(href,location.href); return url.origin===location.origin && ["http:","https:"].includes(url.protocol) && !url.username && !url.password ? url : null; }
    catch { return null; }
  }
  function validateNodes(root) {
    for (const node of [root,...root.querySelectorAll("*")]) for (const attr of node.attributes) {
      if (/^on/i.test(attr.name) || ["srcdoc","style"].includes(attr.name)) throw new Error("Unsafe markup");
      if (["href","src","action","xlink:href"].includes(attr.name) && !canonical(attr.value)) throw new Error("Unsafe URL");
    }
  }
  function restoreFocus() {
    const card = active && findCard(active.id);
    const visible = node => { const r = node?.getBoundingClientRect(); return r && r.top >= 80 && r.top < innerHeight - 44; };
    const equivalent = card && Array.from(card.querySelectorAll("[data-board-action]"))
      .find(node => node.dataset.boardAction===opener?.dataset.boardAction);
    const preferred = opener?.isConnected ? opener : equivalent || card?.querySelector("[data-board-action],h4 a");
    const neighbor = active && document.querySelector(`.board-column[data-group="${active.group}"] h4 a`);
    const nearby = visible(neighbor) ? neighbor : Array.from(document.querySelectorAll("[data-board-root] h4 a")).find(visible);
    (visible(preferred) ? preferred : nearby || document.querySelector(".task-board") || feedback).focus({preventScroll:true});
    if (savedPosition) window.scrollTo(savedPosition);
  }
  function finishClose() {
    const panel = fromPanel, id = active?.id, href = active?.detail_url;
    fromPanel = false; dialog.close();
    if (bodyStyle) { document.body.style.overflow = bodyStyle.overflow; document.body.style.paddingRight = bodyStyle.padding; bodyStyle = null; }
    form.reset(); form.inert=false; discard.hidden = true; restoreFocus();
    document.dispatchEvent(new CustomEvent("ui:validate-files",{detail:{form}}));
    if (panel && !revoked) document.dispatchEvent(new CustomEvent("task-board:action-closed",{detail:{id,href}}));
  }
  const states = new Map();
  const option = (value, text) => {
    const item = document.createElement("option");
    item.value = value; item.textContent = text; return item;
  };
  const chosen = () => active?.actions.find(a => a.key === actionSelect.value);
  function configure() {
    form.querySelectorAll('[aria-invalid="true"]').forEach(node=>{node.removeAttribute("aria-invalid");node.removeAttribute("aria-describedby");});
    const action = chosen();
    submit.disabled = !action || blocked || pending;
    if (!action) {
      assignee.disabled = true; deadline.disabled = true;
      document.querySelector("#board-assignee-field").hidden = true;
      document.querySelector("#board-deadline-field").hidden = true;
      document.querySelector("#board-action-help").textContent = "Выберите действие, затем добавьте обязательный комментарий.";
      submit.textContent = "Подтвердить"; return;
    }
    if (!canonical(action.url)) { blocked=true; submit.disabled=true; return; }
    form.action = action.url;
    submit.textContent = action.label;
    assignee.disabled = !action.assignee; assignee.required = action.assignee;
    document.querySelector("#board-assignee-field").hidden = !action.assignee;
    assignee.replaceChildren();
    for (const user of active.candidates) {
      const item = option(String(user.id), `${user.name} · ${user.role}`);
      item.selected = action.key === "reassign" && active.assignee_ids.includes(user.id);
      assignee.append(item);
    }
    deadline.disabled = !action.deadline; deadline.required = action.deadline;
    document.querySelector("#board-deadline-field").hidden = !action.deadline;
    deadline.value = active.due_at;
    const fileInput=form.querySelector('input[type=file]');
    if (fileInput) fileInput.required=action.key==="upload";
    document.querySelector("#board-action-help").textContent = hints[action.key] || "Действие сохранится в истории вместе с вашим комментарием.";
  }
  function open(card, target = null, preferredKey = null, trigger = null) {
    if (pending || dialog.open || revoked) return;
    if (needsRead) { say("Сначала обновите доску, чтобы проверить результат предыдущего действия."); retryBoard.hidden = false; return; }
    active = states.get(card);
    if (!active) return;
    const actions = target === null ? active.actions : columnActions(active, target);
    if (!actions.length) return;
    opener = trigger || card.querySelector("[data-board-action]");
    phase = "editing"; discard.hidden = true; feedback.hidden = true; savedPosition = {left:window.scrollX,top:window.scrollY};
    form.reset(); comment.readOnly = false;
    form.inert=false; document.dispatchEvent(new CustomEvent("ui:validate-files",{detail:{form}}));
    const upload = form.querySelector('input[type="file"]'); if (upload) upload.disabled = false;
    error.hidden = true; refresh.hidden = true; blocked = false;
    submit.disabled = false; actionSelect.disabled = false;
    document.querySelector("#board-dialog-task").textContent = active.title;
    document.querySelector("#board-revision").value = String(active.revision);
    actionSelect.replaceChildren(...actions.map(a => option(a.key, a.label)));
    if (actions.length > 1) {
      actionSelect.prepend(option("", "Выберите действие")); actionSelect.value = "";
    }
    if (preferredKey && actions.some(a=>a.key===preferredKey)) actionSelect.value=preferredKey;
    bodyStyle = {overflow:document.body.style.overflow,padding:document.body.style.paddingRight};
    const gap = innerWidth - document.documentElement.clientWidth;
    document.body.style.paddingRight = `${(parseFloat(getComputedStyle(document.body).paddingRight)||0)+gap}px`;
    document.body.style.overflow = "hidden";
    configure(); dialog.showModal(); actionSelect.focus({preventScroll:true});
  }
  function close() {
    if (pending) return;
    if (phase !== "posted" && (comment.value || chosen()?.assignee && assignee.value || chosen()?.deadline && deadline.value !== (active?.due_at || "") || form.querySelector('input[type="file"]')?.files.length)) {
      document.querySelector("#board-discard-message").textContent=phase==="uncertain" ? "Действие могло сохраниться. Закрыть текущий ввод без повторной отправки?" : "Комментарий или файлы ещё не отправлены. Закрыть без сохранения?";
      discard.hidden = false; form.inert=true; document.querySelector("#board-keep").focus(); return;
    }
    finishClose();
  }
  function clearTargets() {
    columns.forEach(c => c.classList.remove("board-drop-allowed", "board-drop-active"));
  }
  function markTargets(card) {
    const data = states.get(card);
    columns.forEach(column => {
      if (columnActions(data, column).length)
        column.classList.add("board-drop-allowed");
    });
  }
  // Mouse pointer handling also works in embedded browsers that do not deliver
  // native HTML drag/drop reliably. Touch remains free to scroll the page; buttons
  // provide the same actions without a gesture.
  document.addEventListener("pointermove", event => {
    if (!pointerDrag || event.pointerId !== pointerDrag.id) return;
    if (!pointerDrag.moving && Math.hypot(event.clientX-pointerDrag.x, event.clientY-pointerDrag.y) > 8) {
      pointerDrag.moving = true; source = pointerDrag.card;
      source.classList.add("board-drag-source"); markTargets(source);
    }
    if (!pointerDrag.moving) return;
    const hovered = document.elementFromPoint(event.clientX,event.clientY)?.closest(".board-column");
    columns.forEach(column => column.classList.toggle("board-drop-active", column === hovered && column.classList.contains("board-drop-allowed")));
  });
  function endPointer(event, cancelled = false) {
    if (!pointerDrag || event.pointerId !== pointerDrag.id) return;
    const gesture = pointerDrag; pointerDrag = null;
    const target = document.elementFromPoint(event.clientX,event.clientY)?.closest(".board-column");
    if (gesture.card.hasPointerCapture(event.pointerId)) gesture.card.releasePointerCapture(event.pointerId);
    if (!cancelled && gesture.moving && target?.classList.contains("board-drop-allowed")) open(gesture.card,target);
    else if (!cancelled && gesture.moving) {
      feedback.textContent = "Переход в эту колонку недоступен. Карточка осталась на прежнем этапе."; feedback.hidden = false;
    }
    gesture.card.classList.remove("board-drag-source"); clearTargets(); source = null;
  }
  document.addEventListener("pointerup", event => endPointer(event));
  document.addEventListener("pointercancel", event => endPointer(event,true));
  function bindBoard() {
  states.clear(); columnStates.clear(); source = null; pointerDrag = null;
  columns = Array.from(document.querySelectorAll(".board-column"));
  columns.forEach(column => columnStates.set(column,JSON.parse(column.dataset.statuses)));
  document.querySelectorAll("[data-board-task]").forEach(card => {
    let data;
    try { data = JSON.parse(card.dataset.boardTask); } catch (_) { return; }
    states.set(card, data);
    card.querySelectorAll("[data-board-action]").forEach(link => {
      link.setAttribute("aria-haspopup", "dialog");
      link.addEventListener("click", event => { if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); open(card,null,link.dataset.boardAction,link); });
    });
    const canDrag = columns.some(column => columnActions(data, column).length);
    card.draggable = canDrag;
    if (!canDrag) return;
    card.querySelector("[data-drag-hint]").hidden = false;
    card.addEventListener("pointerdown", event => {
      if (event.pointerType !== "mouse" || event.button !== 0 || pending || dialog.open || event.target.closest("a,button,input,select,textarea")) return;
      event.preventDefault();
      pointerDrag = {card,id:event.pointerId,x:event.clientX,y:event.clientY,moving:false};
      card.setPointerCapture(event.pointerId);
    });
    card.addEventListener("dragstart", event => {
      if (pointerDrag?.card === card) { event.preventDefault(); return; }
      if (event.target.closest("a,button,input,select,textarea") || dialog.open || pending) { event.preventDefault(); return; }
      source = card;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(data.id));
      card.classList.add("board-drag-source");
      markTargets(card);
    });
    card.addEventListener("dragend", () => { clearTargets(); card.classList.remove("board-drag-source"); source = null; });
  });
  columns.forEach(column => {
    const allowed = () => source && columnActions(states.get(source), column).length;
    column.addEventListener("dragover", event => {
      if (!allowed()) return;
      event.preventDefault(); event.dataTransfer.dropEffect = "move";
      column.classList.add("board-drop-active");
    });
    column.addEventListener("dragleave", event => {
      if (!column.contains(event.relatedTarget)) column.classList.remove("board-drop-active");
    });
    column.addEventListener("drop", event => {
      event.preventDefault();
      if (allowed()) open(source, column);
      clearTargets();
    });
  });
  }
  bindBoard();
  actionSelect.addEventListener("change", configure);
  cancel.addEventListener("click", close);
  function requestClose() {
    if (pending) return;
    if (!discard.hidden) {discard.hidden=true;form.inert=false;comment.focus();}
    else close();
  }
  dialog.addEventListener("cancel", event => {
    event.preventDefault(); requestClose();
  });
  dialog.addEventListener("keydown", event => {
    if (event.key==="Escape") {event.preventDefault();event.stopPropagation();requestClose();return;}
    if (event.key !== "Tab") return;
    const items = Array.from(dialog.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]'))
      .filter(item => item.getClientRects().length && !item.closest("[hidden],[inert]"));
    if (!items.includes(document.activeElement)) {
      event.preventDefault(); (event.shiftKey ? items.at(-1) : items[0])?.focus(); return;
    }
    if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus(); }
    else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus(); }
  });
  document.addEventListener("focusin", event => {
    const card = event.target.closest("[data-board-task]");
    if (!card || document.querySelector("dialog[open]")) return;
    const column=card.closest(".board-column"); if (!column) return;
    const heading = column.querySelector(".board-column-heading").getBoundingClientRect();
    const rect = event.target.getBoundingClientRect();
    if (rect.top < heading.bottom + 8) window.scrollBy(0,rect.top - heading.bottom - 12);
  });
  document.querySelector("#board-keep").addEventListener("click", () => { discard.hidden = true; form.inert=false; comment.focus(); });
  document.querySelector("#board-close").addEventListener("click",close);
  document.querySelector("#board-discard-confirm").addEventListener("click", finishClose);
  async function refreshAction() {
    if (pending || revoked) return;
    pending = true; refresh.hidden = true; submit.disabled = true;
    saving.textContent = "Обновляем данные задачи…"; saving.hidden = false; form.setAttribute("aria-busy","true");
    const oldAction = actionSelect.value, oldAssignees = Array.from(assignee.selectedOptions, item => item.value), oldDeadline = deadline.value;
    try {
      await readBoard();
      if (phase === "posted") { say("Действие сохранено. Доска обновлена."); pending = false; finishClose(); return; }
      if (phase === "conflict") {
        const card = findCard(active.id);
        if (!card || !states.get(card)?.actions.length) {
          error.textContent = "Задача больше не доступна для этого действия. Комментарий не отправлен.";
        } else {
          active = states.get(card);
          document.querySelector("#board-dialog-task").textContent = active.title;
          document.querySelector("#board-revision").value = active.revision;
          actionSelect.replaceChildren(option("","Выберите действие"),...active.actions.map(a => option(a.key,a.label)));
          actionSelect.value = active.actions.some(a => a.key === oldAction) ? oldAction : "";
          blocked = false; phase = "editing"; actionSelect.disabled = false; configure();
          for (const item of assignee.options) item.selected = oldAssignees.includes(item.value);
          if (chosen()?.deadline) deadline.value = oldDeadline;
          error.textContent = "Данные обновлены. Комментарий сохранён. Проверьте действие и поля перед отправкой.";
        }
      } else error.textContent = "Доска перечитана. Результат прежней отправки мог быть сохранён; проверьте карточку. Эта форма повторно не отправляется.";
      error.hidden = false;
    } catch (_) { error.textContent = revoked ? "Доступ изменился. Откройте страницу заново." : "Не удалось обновить доску. Повторите только загрузку данных."; error.hidden = false; refresh.hidden = revoked; }
    finally { pending = false; saving.hidden = true; saving.textContent = "Сохраняем действие…"; form.removeAttribute("aria-busy"); submit.disabled = phase !== "editing" || blocked || !chosen(); cancel.disabled = false; }
  }
  refresh.addEventListener("click", event => { if(revoked)return; event.preventDefault(); refreshAction(); });
  retryBoard.addEventListener("click", async () => {
    if (pending || revoked) return; pending = true; retryBoard.disabled = true; retryBoard.textContent = "Обновляем доску…";
    try { await readBoard(); say("Доска обновлена. Проверьте актуальный статус задачи перед новым действием."); }
    catch (_) { say(revoked ? "Доступ изменился. Откройте страницу заново." : "Не удалось обновить доску. Повторите загрузку."); }
    finally { pending = false; retryBoard.disabled = false; retryBoard.textContent = "Повторить обновление доски"; }
  });
  document.addEventListener("task-board:action", async event => {
    if (pending || revoked || dialog.open) return;
    event.preventDefault(); pending = true; say("Обновляем данные для действия…");
    try {
      await readBoard(); pending = false;
      const card = findCard(event.detail.id);
      if (card && states.get(card)?.actions.length) { fromPanel = true; open(card); }
      else document.dispatchEvent(new CustomEvent("task-board:action-closed",{detail:event.detail}));
    } catch (_) { pending = false; if (!revoked) { say("Не удалось обновить данные для действия. Повторите загрузку доски."); retryBoard.hidden = false; } }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending || blocked || phase !== "editing") return;
    document.dispatchEvent(new CustomEvent("ui:validate-files",{detail:{form}}));
    comment.setCustomValidity(comment.value.trim() ? "" : "Добавьте непустой комментарий.");
    if (!form.reportValidity()) return;
    if (!comment.value.trim()) {
      error.textContent = "Добавьте непустой комментарий к действию.";
      error.hidden = false; comment.focus(); return;
    }
    const action = chosen(); if (!action) return;
    const body = new FormData(form);
    pending = true; error.hidden = true; saving.hidden = false; discard.hidden = true;
    submit.setAttribute("data-ui-busy",""); submit.setAttribute("aria-disabled","true");
    form.setAttribute("aria-busy", "true");
    submit.disabled = true; cancel.disabled = true; document.querySelector("#board-close").disabled=true; actionSelect.disabled = true;
    comment.readOnly = true; assignee.disabled = true; deadline.disabled = true;
    const upload = form.querySelector('input[type="file"]'); if (upload) upload.disabled = true;
    saving.focus({preventScroll:true});
    try {
      const response = await fetch(action.url, {method:"POST",body,credentials:"same-origin",headers:{Accept:"text/html"}});
      const finalURL = new URL(response.url);
      const expected=canonical(active.detail_url);
      if ([401,403,404].includes(response.status) || (response.redirected && (!expected || finalURL.origin !== location.origin || finalURL.pathname !== expected.pathname))) {
        blocked = true; phase = "revoked"; purge("Действие недоступно или сеанс завершён. Откройте страницу заново.");
        error.textContent = "Доступ изменился. Данные и несохранённый ввод очищены."; error.hidden = false; error.focus(); return;
      }
      if (response.ok && response.redirected && expected && finalURL.origin === location.origin && finalURL.pathname === expected.pathname) {
        phase = "posted"; blocked = true; needsRead = true;
        saving.textContent = "Действие сохранено. Обновляем доску…";
        try {
          await readBoard(); say(fullMode ? "Действие сохранено. Задача и обсуждение обновлены." : "Действие сохранено. Карточки и счётчики обновлены.");
          pending = false; finishClose();
        } catch (_) {
          error.textContent = revoked ? "Действие сохранено, но доступ к доске изменился. Откройте страницу заново." : "Действие сохранено, но доску обновить не удалось. Повторите только обновление.";
          error.hidden = false; error.focus(); refresh.hidden = revoked; retryBoard.hidden = revoked;
        }
        return;
      }
      const parsed = new DOMParser().parseFromString(await response.text(),"text/html");
      const explanation = parsed.querySelector(".flash.error[role=alert] p")?.textContent?.trim();
      if (response.status === 409) {
        phase = "conflict"; blocked = true; needsRead = true;
        error.textContent = (explanation || "Задача уже изменена.") + " Комментарий сохранён. Перечитайте данные перед новым действием.";
      } else if ([400,413,422].includes(response.status) && !response.redirected) {
        phase = "editing"; blocked = false; error.textContent = (explanation || (response.status===413 ? "Превышен размер запроса. Уменьшите набор файлов и выберите их повторно." : "Проверьте поля формы. Отправка не выполнена.")).slice(0,500);
        const field=parsed.querySelector("[data-error-field]")?.dataset.errorField;
        const input=field && form.elements.namedItem(field);
        if (input) {input.setAttribute("aria-invalid","true");input.setAttribute("aria-describedby","board-error");}
      } else {
        phase = "uncertain"; blocked = true; needsRead = true;
        error.textContent = "Не удалось подтвердить результат отправки. Не отправляйте действие повторно; сначала обновите доску.";
      }
      error.hidden = false; refresh.hidden = !blocked; error.focus();
      const files = form.querySelector('input[type="file"]');
      if (files?.files.length) { files.value = ""; document.dispatchEvent(new CustomEvent("ui:validate-files",{detail:{form}})); error.textContent += " Выберите файлы повторно после проверки данных."; }
    } catch (_) {
      phase = "uncertain"; blocked = true; needsRead = true;
      error.textContent = "Ответ сервера не получен. Действие могло сохраниться. Повторите только загрузку доски.";
      const files = form.querySelector('input[type="file"]');
      if (files?.files.length) { files.value = ""; document.dispatchEvent(new CustomEvent("ui:validate-files",{detail:{form}})); error.textContent += " Перед новым действием выберите файлы повторно."; }
      error.hidden = false; refresh.hidden = false; error.focus();
    } finally {
      pending = false; saving.hidden = true; saving.textContent = "Сохраняем действие…"; form.removeAttribute("aria-busy");
      submit.removeAttribute("data-ui-busy"); submit.removeAttribute("aria-disabled");
      submit.disabled = blocked || phase !== "editing" || !chosen(); cancel.disabled = false; document.querySelector("#board-close").disabled=false; actionSelect.disabled = blocked;
      comment.readOnly = phase === "posted" || revoked;
      const upload = form.querySelector('input[type="file"]'); if (upload) upload.disabled = phase === "posted" || revoked;
      assignee.disabled = blocked || !chosen()?.assignee; deadline.disabled = blocked || !chosen()?.deadline;
      retryBoard.hidden = !needsRead || revoked;
      if (revoked) {refresh.hidden=false;refresh.href=location.href;refresh.textContent="Открыть страницу заново";}
    }
  });
  comment.addEventListener("input",()=>comment.setCustomValidity(""));
})();
