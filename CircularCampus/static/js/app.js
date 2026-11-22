/* Circular Campus — progressive enhancement only. Every feature also works without JS. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // Mobile navigation drawer
  const sidebar = $(".sidebar"), scrim = $(".scrim"), menu = $(".menu-btn");
  function toggleNav(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    scrim && scrim.classList.toggle("show", open);
    menu && menu.setAttribute("aria-expanded", String(open));
  }
  menu && menu.addEventListener("click", () => toggleNav(!sidebar.classList.contains("open")));
  scrim && scrim.addEventListener("click", () => toggleNav(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleNav(false); });

  // Confirmation dialog for consequential actions: <form data-confirm="...">
  const dialog = $("#confirm-dialog");
  let pending = null;
  $$("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (form.dataset.confirmed === "1" || !dialog || typeof dialog.showModal !== "function") return;
      e.preventDefault();
      pending = { form, submitter: e.submitter };
      $("#confirm-title", dialog).textContent = form.dataset.confirmTitle || "Please confirm";
      $("#confirm-text", dialog).textContent = form.dataset.confirm;
      $("#confirm-ok", dialog).textContent = form.dataset.confirmOk || "Confirm";
      dialog.showModal();
    });
  });
  if (dialog) {
    $("#confirm-ok", dialog).addEventListener("click", () => {
      dialog.close();
      if (!pending) return;
      pending.form.dataset.confirmed = "1";
      if (pending.form.requestSubmit) pending.form.requestSubmit(pending.submitter || undefined); else pending.form.submit();
      pending = null;
    });
    $("#confirm-cancel", dialog).addEventListener("click", () => { dialog.close(); pending = null; });
  }

  // Loading state + double-submit guard (the server is idempotent as well)
  $$("form").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (e.defaultPrevented) return;
      const btn = e.submitter;
      if (btn && btn.matches("[data-loading]")) {
        btn.classList.add("is-loading");
        const label = btn.querySelector(".btn-label");
        if (label) label.textContent = btn.dataset.loading;
        setTimeout(() => { btn.setAttribute("disabled", "disabled"); }, 0);
        // keep the pressed button's name/value in the POST
        if (btn.name) { const h = document.createElement("input"); h.type = "hidden"; h.name = btn.name; h.value = btn.value; form.appendChild(h); }
      }
    });
  });

  // Print buttons (inline handlers are blocked by the CSP)
  $$("[data-print]").forEach((b) => b.addEventListener("click", () => window.print()));

  // Dismissible messages
  $$(".toast-close").forEach((b) => b.addEventListener("click", () => b.closest(".alert").remove()));

  // Chart tooltips for marks with data-tip (also reachable by keyboard focus)
  const tip = document.createElement("div");
  tip.className = "tooltip"; tip.setAttribute("role", "status");
  document.body.appendChild(tip);
  function showTip(el, x, y) {
    tip.textContent = "";
    const b = document.createElement("b"); b.textContent = el.dataset.tipValue || "";
    const s = document.createElement("span"); s.textContent = el.dataset.tip || "";
    tip.append(b, s);
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - r.width - 8, Math.max(8, x - r.width / 2)) + "px";
    tip.style.top = Math.max(8, y - r.height - 12) + "px";
    tip.classList.add("show");
  }
  $$("[data-tip]").forEach((el) => {
    el.addEventListener("pointermove", (e) => showTip(el, e.clientX, e.clientY));
    el.addEventListener("pointerleave", () => tip.classList.remove("show"));
    el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); showTip(el, r.left + r.width / 2, r.top); });
    el.addEventListener("blur", () => tip.classList.remove("show"));
  });

  // Assistant suggestions fill the composer
  $$("[data-suggest]").forEach((b) => b.addEventListener("click", () => {
    const input = $("#question"); if (!input) return;
    input.value = b.dataset.suggest; input.focus();
  }));
  const latest = $("#latest"); if (latest) latest.scrollIntoView({ block: "center" });

  // Demo sign-in helper: fills the campus ID only (password typed by the presenter)
  $$("[data-fill-id]").forEach((b) => b.addEventListener("click", () => {
    const f = $("#id_campus_id"); if (f) { f.value = b.dataset.fillId; $("#id_password").focus(); }
  }));

  // Auto-submit filter selects when JS is available (form still has a submit button)
  $$("form[data-autosubmit] select").forEach((s) => s.addEventListener("change", () => s.form.requestSubmit ? s.form.requestSubmit() : s.form.submit()));
})();
