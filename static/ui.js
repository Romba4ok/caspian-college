/* Shared progressive UI only. Server-provided actions remain authoritative. */
(() => {
  "use strict";
  document.documentElement.classList.add("js");
  document.querySelectorAll("[data-password-toggle]").forEach(button => {
    const input = document.getElementById(button.getAttribute("aria-controls"));
    if (!input || input.type !== "password") return;
    button.hidden = false;
    button.addEventListener("click", () => {
      const showing = input.type === "password";
      input.type = showing ? "text" : "password";
      button.setAttribute("aria-label", showing ? "Скрыть пароль" : "Показать пароль");
      button.setAttribute("aria-pressed", String(showing));
      button.querySelector("[data-password-show-icon]").hidden = showing;
      button.querySelector("[data-password-hide-icon]").hidden = !showing;
    });
  });
  const toggle = document.querySelector("[data-nav-toggle]");
  const navigation = document.querySelector("#primary-navigation");
  function closeNavigation(restoreFocus) {
    if (!toggle || !navigation) return;
    navigation.classList.remove("is-open");
    toggle.setAttribute("aria-expanded", "false");
    if (restoreFocus) toggle.focus();
  }
  toggle?.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    navigation?.classList.toggle("is-open", open);
  });
  const mobile = window.matchMedia("(max-width:1023px)");
  mobile.addEventListener("change", () => closeNavigation(mobile.matches && navigation?.contains(document.activeElement)));
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const account = document.querySelector(".account-menu[open]");
    if (account) {
      account.open = false;
      account.querySelector("summary")?.focus();
    } else if (toggle?.getAttribute("aria-expanded") === "true") closeNavigation(true);
  });
  document.addEventListener("click", event => {
    const account = document.querySelector(".account-menu[open]");
    if (account && !account.contains(event.target)) account.open = false;
    const link = event.target.closest("[data-error-summary] a[href^='#']");
    if (link) {
      const target = document.getElementById(link.getAttribute("href").slice(1));
      if (target) { event.preventDefault(); target.focus(); target.scrollIntoView({block:"center"}); }
    }
  });

  const pending = new Map();
  const accountDialog=document.querySelector("#account-confirm");
  if(typeof accountDialog?.showModal==="function")document.querySelectorAll("[data-delete-fallback]").forEach(details=>{
    details.open=true;details.dataset.uiEnhanced="true";
    details.querySelector("[data-button-label]").textContent="Удалить";
  });
  let accountDecision=null,accountOverflow="",accountLeaving=false;
  function confirmAccount(decision,title,message,label,keepLabel="Отмена") {
    if (!accountDialog || typeof accountDialog.showModal!=="function") return false;
    accountDecision=decision;
    accountDialog.querySelector("#account-confirm-title").textContent=title;
    accountDialog.querySelector("#account-confirm-message").textContent=message;
    accountDialog.querySelector("[data-confirm-accept]").textContent=label;
    accountDialog.querySelector("[data-confirm-keep]").textContent=keepLabel;
    accountOverflow=document.body.style.overflow;document.body.style.overflow="hidden";
    accountDialog.showModal();accountDialog.querySelector("[data-confirm-keep]").focus();return true;
  }
  function keepAccount() {
    const trigger=accountDecision?.trigger;accountDecision=null;
    accountDialog.close();document.body.style.overflow=accountOverflow;trigger?.focus({preventScroll:true});
  }
  accountDialog?.querySelector("[data-confirm-keep]").addEventListener("click",keepAccount);
  accountDialog?.addEventListener("cancel",event=>{event.preventDefault();keepAccount();});
  accountDialog?.addEventListener("keydown",event=>{
    if(event.key==="Escape"){event.preventDefault();event.stopPropagation();keepAccount();return;}
    if(event.key!=="Tab")return;
    const controls=[...accountDialog.querySelectorAll("button")];
    if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1).focus();}
    else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0].focus();}
  });
  accountDialog?.querySelector("[data-confirm-accept]").addEventListener("click",()=>{
    const decision=accountDecision;if(!decision)return;
    accountDecision=null;accountDialog.close();document.body.style.overflow=accountOverflow;
    if(decision.form){decision.form.dataset.uiConfirmed="true";decision.form.requestSubmit(decision.trigger);}
    else {accountLeaving=true;location.assign(decision.href);}
  });
  const accountForm=document.querySelector("form[data-account-form],form[data-department-form]");
  const accountDirty=()=>accountForm&&(accountForm.dataset.accountUnsaved==="true"||[...accountForm.elements].some(input=>{
    if(!input.name||input.type==="hidden"||input.type==="submit")return false;
    const original=input.tagName==="SELECT" ? [...input.options].find(option=>option.defaultSelected)?.value ?? input.options[0]?.value : input.defaultValue;
    return input.value!==original;
  }));
  document.addEventListener("click",event=>{
    const link=event.target.closest("[data-account-cancel],[data-department-cancel]");
    if(!link||event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||!accountDirty()||pending.has(accountForm))return;
    const subject=accountForm.hasAttribute("data-department-form") ? "Отдел" : "Учётная запись";
    if(confirmAccount({href:link.href,trigger:link},"Закрыть форму?","Введённые изменения не сохранятся. "+subject+" останется без изменений.","Закрыть без сохранения","Продолжить редактирование"))event.preventDefault();
  });
  window.addEventListener("beforeunload",event=>{if(accountDirty()&&!accountLeaving&&!pending.has(accountForm)){event.preventDefault();event.returnValue="";}});
  let summaryNumber = 0;
  function showErrors(form, invalid) {
    let summary = form.querySelector("[data-error-summary]");
    if (!summary && !form.hasAttribute("data-inline-errors-only")) {
      summary = document.createElement("div");
      summary.className = "error-summary";
      summary.dataset.errorSummary = "";
      summary.setAttribute("role", "alert");
      summary.tabIndex = -1;
      const title = document.createElement("h3");
      title.id = "ui-errors-" + (++summaryNumber);
      title.textContent = "Проверьте заполнение формы";
      summary.setAttribute("aria-labelledby", title.id);
      summary.append(title, document.createElement("ul"));
      form.prepend(summary);
    }
    const list = summary?.querySelector("ul");
    list?.replaceChildren();
    form.querySelectorAll("[data-field-label]").forEach(input => {
      const errorId = input.id + "-error";
      const note = document.getElementById(errorId);
      const isInvalid = invalid.includes(input);
      input.removeAttribute("aria-invalid");
      const descriptions = (input.getAttribute("aria-describedby") || "").split(" ").filter(id => id && id !== errorId);
      if (note) { note.hidden = !isInvalid; note.textContent = ""; }
      if (isInvalid) {
        const label = input.dataset.fieldLabel || input.name;
        const text = input.validity.customError ? input.validationMessage : (input.validity.valueMissing ? "Заполните поле «" + label + "»." : "Проверьте поле «" + label + "».");
        input.setAttribute("aria-invalid", "true");
        if (note) { note.hidden = false; note.textContent = text; descriptions.push(errorId); }
        const item = document.createElement("li"), link = document.createElement("a");
        link.href = "#" + input.id;
        link.textContent = text;
        item.append(link); list?.append(item);
      }
      if (descriptions.length) input.setAttribute("aria-describedby", descriptions.join(" "));
      else input.removeAttribute("aria-describedby");
    });
    if (summary) summary.hidden = invalid.length === 0;
    if (invalid.length) (summary || invalid[0]).focus();
  }
  function restoreForms() {
    accountLeaving=false;
    for (const [form, state] of pending) {
      delete form.dataset.uiSubmitting;
      if (state.button) {
        state.button.removeAttribute("aria-disabled");
        state.button.removeAttribute("data-ui-busy");
        if (state.label) state.label.textContent = state.text;
      }
      const status = form.querySelector("[data-form-pending]");
      if (status) status.textContent = "";
    }
    pending.clear();
  }
  function validateFiles(input) {
    const files = [...input.files];
    const allowed = input.dataset.extensions.split(",");
    let message = "";
    if (files.some(file => !allowed.includes(file.name.split(".").pop().toLowerCase())))
      message = "Допустимы PDF, DOCX, XLSX, PPTX, JPG, PNG. Выберите файлы повторно.";
    else if (files.some(file => file.size > Number(input.dataset.maxMb)*1024*1024))
      message = "Размер файла превышает 20 МБ. Выберите файлы повторно.";
    else if (files.reduce((sum,file) => sum+file.size,0)>21*1024*1024)
      message = "Суммарный размер файлов превышает 21 МБ. Уменьшите набор файлов.";
    input.setCustomValidity(message);
    const list = input.closest("[data-field]").querySelector("[data-selected-files]");
    list.replaceChildren(...files.map(file => {
      const item = document.createElement("li");
      item.textContent = file.name + " · " + Math.max(1,Math.ceil(file.size/1024)) + " КБ";
      return item;
    }));
  }
  document.addEventListener("change",event=>{if(event.target.matches("[data-file-policy]")) validateFiles(event.target);});
  document.addEventListener("ui:validate-files",event=>event.detail?.form?.querySelectorAll("[data-file-policy]").forEach(validateFiles));
  document.querySelectorAll("form[data-ui-form]").forEach(form => {
    form.noValidate = true;
    form.addEventListener("input",event => {
      if ((form.hasAttribute("data-task-create")||form.matches("[data-account-form],[data-department-form]")) && event.target.matches("input:not([type=file]),textarea"))
        event.target.setCustomValidity("");
    });
    form.addEventListener("submit", event => {
      if (pending.has(form)) { event.preventDefault(); return; }
      if (form.hasAttribute("data-task-create")||form.matches("[data-account-form],[data-department-form]")) {
        form.querySelectorAll("[required]").forEach(input => {
          if (input.matches('input[type=text],textarea')) input.setCustomValidity(input.value.trim() ? "" : "Заполните поле «" + input.dataset.fieldLabel + "».");
        });
        form.querySelectorAll("[data-file-policy]").forEach(validateFiles);
      }
      const invalid = Array.from(form.elements).filter(input => input.willValidate && !input.validity.valid);
      if (invalid.length) { event.preventDefault(); showErrors(form, invalid); return; }
      const summary = form.querySelector("[data-error-summary]");
      if (summary || form.hasAttribute("data-inline-errors-only")) showErrors(form, []);
      if (form.dataset.confirmMessage && form.dataset.uiConfirmed!=="true") {
        if(confirmAccount({form,trigger:event.submitter},form.dataset.confirmTitle,form.dataset.confirmMessage,form.dataset.confirmLabel)){event.preventDefault();return;}
      }
      delete form.dataset.uiConfirmed;
      queueMicrotask(() => {
        if (event.defaultPrevented) return;
        const button = event.submitter || form.querySelector("button[type=submit]");
        const label = button?.querySelector("[data-button-label]");
        pending.set(form, {button, label, text:label?.textContent});
        form.dataset.uiSubmitting = "true";
        // Keep named submitters successful controls; reject a second submit above.
        button?.setAttribute("aria-disabled", "true");
        button?.setAttribute("data-ui-busy", "");
        if (label) label.textContent = button.dataset.busyLabel || "Отправляем…";
        const status = form.querySelector("[data-form-pending]");
        if (status) status.textContent = form.method === "get" ? "Применяем фильтры…" : "Отправляем форму…";
      });
    });
  });
  window.addEventListener("pageshow", restoreForms);
  const existingError = document.querySelector("[data-error-summary]:not([hidden])");
  existingError?.focus();
  document.querySelector("form[data-inline-errors-only] [aria-invalid=true]")?.focus();
  document.querySelector("[data-creation-receipt]")?.focus({preventScroll:true});
  document.querySelector("[data-notification-feedback]")?.focus({preventScroll:true});
  const createForm = document.querySelector("form[data-task-create]");
  if (createForm) {
    const discard = document.querySelector("#create-discard");
    let leaving = false, destination, trigger;
    const dirty = () => [...createForm.elements].some(input => {
      if (!input.name || input.type === "hidden" || input.type === "submit") return false;
      if (input.type === "file") return input.files.length > 0;
      return input.name === "priority" ? input.value !== "normal" : !!input.value.trim();
    });
    function keep() { discard.close(); trigger?.focus({preventScroll:true}); }
    document.addEventListener("click",event => {
      const link = event.target.closest("[data-create-cancel],.create-breadcrumb a");
      if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
          !dirty() || pending.has(createForm) || typeof discard?.showModal !== "function") return;
      event.preventDefault();
      destination = link.href; trigger = link;
      discard.showModal();
      discard.querySelector("[data-create-keep]").focus();
    });
    discard?.querySelector("[data-create-keep]").addEventListener("click",keep);
    discard?.addEventListener("cancel",event => { event.preventDefault(); keep(); });
    discard?.addEventListener("keydown",event => {
      if (event.key !== "Tab") return;
      const controls = [...discard.querySelectorAll("button")];
      if (event.shiftKey && document.activeElement === controls[0]) {
        event.preventDefault(); controls.at(-1)?.focus();
      } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
        event.preventDefault(); controls[0]?.focus();
      }
    });
    discard?.querySelector("[data-create-leave]").addEventListener("click",() => {
      leaving = true; discard.close(); location.assign(destination);
    });
    window.addEventListener("beforeunload",event => {
      if (dirty() && !leaving && !pending.has(createForm)) { event.preventDefault(); event.returnValue = ""; }
    });
  }
})();
