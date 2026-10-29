/* Board UI only: the existing workflow endpoints remain authoritative. */
(() => {
  "use strict";
  const viewSwitch = document.querySelector("[data-task-view]");
  if (!viewSwitch) return;
  // Plain task URLs always use the server default (board); ?view=list is explicit.
  const dialog = document.querySelector("#board-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;
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
  const initialUser = document.querySelector("[data-board-root]").dataset.userId;
  const initialDirection = viewSwitch.dataset.direction;
  const readURL = document.querySelector("[data-board-root]").dataset.refreshUrl;
  const retryBoard = document.querySelector("#board-retry");
  const discard = document.querySelector("#board-discard");
  const say = message => { feedback.textContent = message; feedback.hidden = false; };
  function purge(message) {
    revoked = true; needsRead = true; states.clear();
    const root = document.querySelector("[data-board-root]");
    root?.replaceChildren();
    if (root) for (const name of ["data-counts","data-total","data-csrf-token","data-user-id","data-refresh-url"]) root.removeAttribute(name);
    document.querySelectorAll(".task-list-heading .hint,.task-page-heading .hint").forEach(e => { e.textContent = ""; });
    active = null; source = null; pointerDrag = null; columns = []; columnStates.clear();
    document.querySelectorAll(".task-tab-count").forEach(e => { e.textContent = "—"; e.removeAttribute("aria-label"); });
    document.dispatchEvent(new CustomEvent("task-board:revoked"));
    form.reset(); actionSelect.replaceChildren(); assignee.replaceChildren();
    form.querySelector('[name="csrf_token"]').value = "";
    document.querySelector("#board-dialog-task").textContent = "Задача недоступна";
    say(message); retryBoard.hidden = true;
  }
  document.addEventListener("task-panel:revoked", event => {
    purge(event.detail.message);
    feedback.focus({preventScroll:true});
  });
  async function readBoard() {
    const url = new URL(readURL, location.href);
    if (url.origin !== location.origin) throw new Error("Invalid origin");
    const response = await fetch(url, {credentials:"same-origin", cache:"no-store", redirect:"manual", headers:{Accept:"text/html"}});
    if (response.type === "opaqueredirect" || [401,403,404].includes(response.status)) {
      purge("Доступ или сеанс изменился. Откройте страницу заново и войдите при необходимости.");
      throw new Error("Access changed");
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
  function restoreFocus() {
    const card = active && document.querySelector(`[data-task-id="${active.id}"]`);
    const visible = node => { const r = node?.getBoundingClientRect(); return r && r.top >= 80 && r.top < innerHeight - 44; };
    const preferred = card?.querySelector("[data-board-action],h4 a");
    const neighbor = active && document.querySelector(`.board-column[data-group="${active.group}"] h4 a`);
    const nearby = visible(neighbor) ? neighbor : Array.from(document.querySelectorAll("[data-board-root] h4 a")).find(visible);
    (visible(preferred) ? preferred : nearby || document.querySelector(".task-board") || feedback).focus({preventScroll:true});
    if (savedPosition) window.scrollTo(savedPosition);
  }
  function finishClose() {
    const panel = fromPanel, id = active?.id, href = active?.detail_url;
    fromPanel = false; dialog.close();
    if (bodyStyle) { document.body.style.overflow = bodyStyle.overflow; document.body.style.paddingRight = bodyStyle.padding; bodyStyle = null; }
    form.reset(); discard.hidden = true; restoreFocus();
    if (panel && !revoked) document.dispatchEvent(new CustomEvent("task-board:action-closed",{detail:{id,href}}));
  }
  const states = new Map();
  const option = (value, text) => {
    const item = document.createElement("option");
    item.value = value; item.textContent = text; return item;
  };
  const chosen = () => active?.actions.find(a => a.key === actionSelect.value);
  function configure() {
    const action = chosen();
    submit.disabled = !action || blocked || pending;
    if (!action) {
      assignee.disabled = true; deadline.disabled = true;
      document.querySelector("#board-assignee-field").hidden = true;
      document.querySelector("#board-deadline-field").hidden = true;
      document.querySelector("#board-action-help").textContent = "Выберите действие, затем добавьте обязательный комментарий.";
      submit.textContent = "Подтвердить"; return;
    }
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
    const hints = {
      assign: "Назначение сразу переводит задачу в «В работе».",
      claim: "Вы присоединитесь к команде, остальные исполнители сохранятся.",
      reassign: "Меняется состав своего отдела; статус и материалы сохраняются.",
      release: "Вы снимете только себя. После последнего участника задача станет «Новой», иначе работа продолжится.",
      complete: "Задача сразу станет выполненной и перейдёт в «Завершённые». Отправитель сможет вернуть её на доработку.",
      cancel: "Задача будет окончательно отменена. История сохраняется.",
      reject: "Задача будет окончательно отклонена. Объясните причину отправителю.",
      return: "Задача вернётся прежним доступным исполнителям в «В работе» с пометкой «На доработке».",
      deadline: "Снять срок нельзя. Старое и новое значения сохраняются в истории."
    };
    document.querySelector("#board-action-help").textContent = hints[action.key] || "Действие сохранится в истории вместе с вашим комментарием.";
  }
  function open(card, target = null) {
    if (pending || dialog.open || revoked) return;
    if (needsRead) { say("Сначала обновите доску, чтобы проверить результат предыдущего действия."); retryBoard.hidden = false; return; }
    active = states.get(card);
    if (!active) return;
    const actions = target === null ? active.actions : columnActions(active, target);
    if (!actions.length) return;
    opener = card.querySelector("[data-board-action]");
    phase = "editing"; discard.hidden = true; feedback.hidden = true; savedPosition = {left:window.scrollX,top:window.scrollY};
    form.reset(); comment.readOnly = false;
    const upload = form.querySelector('input[type="file"]'); if (upload) upload.disabled = false;
    error.hidden = true; refresh.hidden = true; blocked = false;
    submit.disabled = false; actionSelect.disabled = false;
    document.querySelector("#board-dialog-task").textContent = active.title;
    document.querySelector("#board-revision").value = String(active.revision);
    actionSelect.replaceChildren(...actions.map(a => option(a.key, a.label)));
    if (actions.length > 1) {
      actionSelect.prepend(option("", "Выберите действие")); actionSelect.value = "";
    }
    bodyStyle = {overflow:document.body.style.overflow,padding:document.body.style.paddingRight};
    const gap = innerWidth - document.documentElement.clientWidth;
    document.body.style.paddingRight = `${(parseFloat(getComputedStyle(document.body).paddingRight)||0)+gap}px`;
    document.body.style.overflow = "hidden";
    configure(); dialog.showModal(); actionSelect.focus({preventScroll:true});
  }
  function close() {
    if (pending) return;
    if (phase !== "posted" && (comment.value || form.querySelector('input[type="file"]')?.files.length)) {
      discard.hidden = false; document.querySelector("#board-keep").focus(); return;
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
    const link = card.querySelector("[data-board-action]");
    if (link) {
      link.setAttribute("aria-haspopup", "dialog");
      link.addEventListener("click", event => { if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); open(card); });
    }
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
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  dialog.addEventListener("keydown", event => {
    if (event.key !== "Tab") return;
    const items = Array.from(dialog.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]'))
      .filter(item => item.getClientRects().length && !item.closest("[hidden]"));
    if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus(); }
    else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus(); }
  });
  document.addEventListener("focusin", event => {
    const card = event.target.closest("[data-board-task]");
    if (!card || document.querySelector("dialog[open]")) return;
    const heading = card.closest(".board-column").querySelector(".board-column-heading").getBoundingClientRect();
    const rect = event.target.getBoundingClientRect();
    if (rect.top < heading.bottom + 8) window.scrollBy(0,rect.top - heading.bottom - 12);
  });
  document.querySelector("#board-keep").addEventListener("click", () => { discard.hidden = true; comment.focus(); });
  document.querySelector("#board-discard-confirm").addEventListener("click", finishClose);
  async function refreshAction() {
    if (pending || revoked) return;
    pending = true; refresh.hidden = true; submit.disabled = true;
    saving.textContent = "Обновляем данные задачи…"; saving.hidden = false; form.setAttribute("aria-busy","true");
    const oldAction = actionSelect.value, oldAssignees = Array.from(assignee.selectedOptions, o => o.value), oldDeadline = deadline.value;
    try {
      await readBoard();
      if (phase === "posted") { say("Действие сохранено. Доска обновлена."); pending = false; finishClose(); return; }
      if (phase === "conflict") {
        const card = document.querySelector(`[data-task-id="${active.id}"]`);
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
      } else if (phase === "retired") error.textContent = "Данные обновлены. Закройте старую форму и откройте доступное действие заново.";
      else error.textContent = "Доска перечитана. Результат прежней отправки мог быть сохранён; проверьте карточку. Эта форма повторно не отправляется.";
      error.hidden = false; error.focus();
    } catch (_) { error.textContent = revoked ? "Доступ изменился. Откройте страницу заново." : "Не удалось обновить доску. Повторите только загрузку данных."; error.hidden = false; refresh.hidden = revoked; error.focus(); }
    finally { pending = false; saving.hidden = true; saving.textContent = "Сохраняем действие…"; form.removeAttribute("aria-busy"); submit.disabled = phase !== "editing" || blocked || !chosen(); cancel.disabled = false; }
  }
  refresh.addEventListener("click", event => { event.preventDefault(); refreshAction(); });
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
      const card = document.querySelector(`[data-task-id="${event.detail.id}"]`);
      if (card && states.get(card)?.actions.length) { fromPanel = true; open(card); }
      else document.dispatchEvent(new CustomEvent("task-board:action-closed",{detail:event.detail}));
    } catch (_) { pending = false; if (!revoked) { say("Не удалось обновить данные для действия. Повторите загрузку доски."); retryBoard.hidden = false; } }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending || blocked || phase !== "editing" || !form.reportValidity()) return;
    if (!comment.value.trim()) {
      error.textContent = "Добавьте непустой комментарий к действию.";
      error.hidden = false; comment.focus(); return;
    }
    const action = chosen(); if (!action) return;
    const body = new FormData(form);
    pending = true; error.hidden = true; saving.hidden = false; discard.hidden = true;
    form.setAttribute("aria-busy", "true");
    submit.disabled = true; cancel.disabled = true; actionSelect.disabled = true;
    comment.readOnly = true; assignee.disabled = true; deadline.disabled = true;
    const upload = form.querySelector('input[type="file"]'); if (upload) upload.disabled = true;
    try {
      const response = await fetch(action.url, {method:"POST",body,credentials:"same-origin",headers:{Accept:"text/html"}});
      const finalURL = new URL(response.url);
      if ([401,403,404].includes(response.status) || (response.redirected && (finalURL.origin !== location.origin || finalURL.pathname !== active.detail_url))) {
        blocked = true; phase = "revoked"; purge("Действие недоступно или сеанс завершён. Откройте страницу заново.");
        error.textContent = "Доступ изменился. Данные и несохранённый ввод очищены."; error.hidden = false; return;
      }
      if (response.ok && response.redirected && finalURL.origin === location.origin && finalURL.pathname === active.detail_url) {
        phase = "posted"; blocked = true; needsRead = true;
        saving.textContent = "Действие сохранено. Обновляем доску…";
        try {
          await readBoard(); say("Действие сохранено. Карточки и счётчики обновлены.");
          pending = false; finishClose();
        } catch (_) {
          error.textContent = revoked ? "Действие сохранено, но доступ к доске изменился. Откройте страницу заново." : "Действие сохранено, но доску обновить не удалось. Повторите только обновление.";
          error.hidden = false; refresh.hidden = revoked; retryBoard.hidden = revoked;
        }
        return;
      }
      const parsed = new DOMParser().parseFromString(await response.text(),"text/html");
      const fieldError = parsed.querySelector(".flash.error[role=alert] p");
      const explanation = (fieldError?.textContent || parsed.body.textContent)?.trim();
      if (response.status === 410) {
        phase = "retired"; blocked = true; needsRead = true;
        error.textContent = "Это действие больше не используется. Комментарий не отправлен. Обновите страницу и выберите доступное действие.";
      } else if (response.status === 409) {
        phase = "conflict"; blocked = true; needsRead = true;
        error.textContent = "Задача уже изменена. Комментарий сохранён. Обновите данные перед новым действием.";
      } else if (response.status === 400 && !fieldError) {
        phase = "conflict"; blocked = true; needsRead = true;
        error.textContent = "Не удалось проверить форму. Комментарий сохранён. Обновите данные перед новым действием.";
      } else if ([400,413,422].includes(response.status) && !response.redirected && explanation) {
        phase = "editing"; blocked = false; error.textContent = explanation.slice(0,500);
      } else {
        phase = "uncertain"; blocked = true; needsRead = true;
        error.textContent = "Не удалось подтвердить результат отправки. Не отправляйте действие повторно; сначала обновите доску.";
      }
      error.hidden = false; refresh.hidden = !blocked; error.focus();
      const files = form.querySelector('input[type="file"]');
      if (files?.files.length) { files.value = ""; error.textContent += " Выберите файлы повторно после проверки данных."; }
    } catch (_) {
      phase = "uncertain"; blocked = true; needsRead = true;
      error.textContent = "Ответ сервера не получен. Действие могло сохраниться. Повторите только загрузку доски.";
      const files = form.querySelector('input[type="file"]');
      if (files?.files.length) { files.value = ""; error.textContent += " Перед новым действием выберите файлы повторно."; }
      error.hidden = false; refresh.hidden = false; error.focus();
    } finally {
      pending = false; saving.hidden = true; saving.textContent = "Сохраняем действие…"; form.removeAttribute("aria-busy");
      submit.disabled = blocked || phase !== "editing" || !chosen(); cancel.disabled = false; actionSelect.disabled = blocked;
      comment.readOnly = phase === "posted" || revoked;
      const upload = form.querySelector('input[type="file"]'); if (upload) upload.disabled = phase === "posted" || revoked;
      assignee.disabled = blocked || !chosen()?.assignee; deadline.disabled = blocked || !chosen()?.deadline;
      retryBoard.hidden = !needsRead || revoked;
    }
  });
})();
