(() => {
  const dataEl = document.getElementById("menu-data");
  if (!dataEl) return;
  const menu = JSON.parse(dataEl.textContent);   // { v: {variant id: {...}}, g: {group id: {...}} }
  const key = "cart:" + document.body.dataset.slug;
  const isOpen = document.body.dataset.open !== "0";
  // cart: { "12:3.7": {v: 12, o: [3, 7], q: 2} } — one line per variant + chosen add-ons
  let cart = {};

  const $ = (id) => document.getElementById(id);
  const money = (g) => Math.floor(g / 100) + "," + String(g % 100).padStart(2, "0") + " zł";
  const save = () => { try { localStorage.setItem(key, JSON.stringify(cart)); } catch (e) {} };
  const lineKey = (v, o) => v + ":" + o.slice().sort((a, b) => a - b).join(".");
  const hasGroups = (v) => menu.v[v] && menu.v[v].g.length > 0;
  const optionOf = (id) => {
    for (const g of Object.values(menu.g)) for (const o of g.options) if (o.id === id) return o;
    return null;
  };
  // A saved line is valid when the variant exists and all its add-ons belong to groups of that variant.
  function valid(line) {
    const v = menu.v[line.v];
    if (!v || !Array.isArray(line.o) || !(line.q > 0)) return false;
    const allowed = new Set();
    v.g.forEach((gid) => menu.g[gid].options.forEach((o) => allowed.add(o.id)));
    if (!line.o.every((id) => allowed.has(id))) return false;
    return v.g.every((gid) => {
      const g = menu.g[gid], n = g.options.filter((o) => line.o.includes(o.id)).length;
      return !(g.required && n === 0) && !(!g.multi && n > 1);
    });
  }
  const unit = (line) => menu.v[line.v].price + line.o.reduce((s, id) => s + (optionOf(id) ? optionOf(id).price : 0), 0);
  function label(line) {
    const v = menu.v[line.v];
    const extra = line.o.map((id) => optionOf(id).name);
    return v.name + (v.label || extra.length ? " (" + [v.label, ...extra].filter(Boolean).join(", ") + ")" : "");
  }

  function load() {
    let raw = {};
    try { raw = JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { raw = {}; }
    for (const [k, val] of Object.entries(raw)) {
      const line = typeof val === "number" ? { v: Number(k), o: [], q: val } : val;   // carts saved by older versions
      if (line && valid(line)) cart[lineKey(line.v, line.o)] = { v: line.v, o: line.o, q: Math.min(20, line.q) };
    }
  }

  function el(tag, props = {}, ...kids) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...kids);
    return node;
  }
  function stepBtn(text, k, delta, text2) {
    const b = el("button", { type: "button", textContent: text });
    b.dataset.line = k; b.dataset.delta = delta;
    b.setAttribute("aria-label", text2);
    return b;
  }

  function change(k, delta) {
    const line = cart[k];
    if (!line) return;
    line.q = Math.min(20, line.q + delta);
    if (line.q <= 0) delete cart[k];
    save();
    render();
  }
  function addLine(v, o, q = 1) {
    const k = lineKey(v, o);
    cart[k] = { v, o: o.slice().sort((a, b) => a - b), q: Math.min(20, ((cart[k] && cart[k].q) || 0) + q) };
    save();
    render();
  }

  function renderControls() {
    document.querySelectorAll(".ctl").forEach((ctl) => {
      const v = Number(ctl.dataset.variant);
      ctl.replaceChildren();
      if (!menu.v[v]) return;
      if (hasGroups(v)) {          // needs choices: opens the add-on dialog
        const n = Object.values(cart).filter((l) => l.v === v).reduce((s, l) => s + l.q, 0);
        const b = el("button", { type: "button", className: "add", textContent: n ? "Add (" + n + ")" : "Add" });
        b.dataset.options = v; b.disabled = !isOpen;
        b.setAttribute("aria-label", "Choose options for " + menu.v[v].name);
        ctl.className = "ctl"; ctl.append(b);
        return;
      }
      const k = lineKey(v, []), qty = cart[k] ? cart[k].q : 0;
      if (!qty) {
        const b = stepBtn("Add", k, 1, "Add " + menu.v[v].name);
        b.className = "add"; b.disabled = !isOpen;
        ctl.className = "ctl"; ctl.append(b);
      } else {
        ctl.className = "ctl stepper";
        ctl.append(stepBtn("−", k, -1, "Remove one " + menu.v[v].name), el("span", { textContent: qty }),
                   stepBtn("+", k, 1, "Add one " + menu.v[v].name));
      }
    });
    document.querySelectorAll(".item[data-variants]").forEach((li) => {
      const badge = li.querySelector(".incart");
      if (!badge) return;
      const ids = li.dataset.variants.split(",").map(Number);
      const n = Object.values(cart).filter((l) => ids.includes(l.v)).reduce((s, l) => s + l.q, 0);
      badge.hidden = n === 0;
      badge.textContent = n + " in order";
    });
  }

  function render() {
    const lines = $("cart-lines");
    lines.replaceChildren();
    let total = 0, count = 0;
    for (const [k, line] of Object.entries(cart)) {
      total += unit(line) * line.q;
      count += line.q;
      lines.append(el("li", {},
        el("span", { textContent: label(line) }),
        el("strong", { textContent: money(unit(line) * line.q) }),
        el("span", { className: "qty" },
          stepBtn("−", k, -1, "Remove one " + label(line)), el("span", { textContent: line.q }),
          stepBtn("+", k, 1, "Add one " + label(line)))));
    }
    $("cart-empty").hidden = count > 0;
    $("cart-total").textContent = money(total);
    const submit = $("submit-order"), json = $("cart-json"); // absent when the visitor is not logged in
    if (submit) submit.disabled = count === 0 || !isOpen;
    if (json) json.value = JSON.stringify(Object.values(cart).map((l) => ({ variant_id: l.v, qty: l.q, options: l.o })));
    $("cart-bar").hidden = count === 0;
    $("cart-bar-count").textContent = count + (count === 1 ? " item" : " items");
    $("cart-bar-total").textContent = money(total);
    renderControls();
  }

  // ---- add-on dialog (milk, syrup, ...)
  const dialog = $("opt-dialog");
  function openOptions(v) {
    if (!dialog || !dialog.showModal) return;
    const info = menu.v[v];
    $("opt-title").textContent = info.name + (info.label ? " (" + info.label + ")" : "");
    const box = $("opt-groups");
    box.replaceChildren();
    info.g.forEach((gid) => {
      const g = menu.g[gid];
      const set = el("fieldset", {}, el("legend", { textContent: g.name + (g.required ? "" : " (optional)") }));
      const type = g.multi ? "checkbox" : "radio";
      const add = (value, text, price, checked) => {
        const input = el("input", { type, name: "g" + gid, value, checked });
        input.dataset.price = price;
        set.append(el("label", { className: "opt" }, input, el("span", { textContent: text }),
          el("em", { textContent: price ? "+" + money(price) : "" })));
      };
      if (!g.multi && !g.required) add("", "None", 0, true);
      g.options.forEach((o, i) => add(String(o.id), o.name, o.price, !g.multi && g.required && i === 0));
      box.append(set);
    });
    dialog.dataset.variant = v;
    updatePrice();
    $("opt-error").hidden = true;
    dialog.showModal();
  }
  function chosen() {
    return Array.from(document.querySelectorAll("#opt-groups input:checked")).filter((i) => i.value).map((i) => Number(i.value));
  }
  function updatePrice() {
    const v = Number(dialog.dataset.variant);
    $("opt-price").textContent = money(menu.v[v].price + chosen().reduce((s, id) => s + optionOf(id).price, 0));
  }
  if (dialog) {
    dialog.addEventListener("change", updatePrice);
    $("opt-form").addEventListener("submit", (e) => {
      if (e.submitter && e.submitter.value === "cancel") return;     // closes the dialog
      e.preventDefault();
      const v = Number(dialog.dataset.variant), o = chosen();
      if (!valid({ v, o, q: 1 })) {
        $("opt-error").textContent = "Please choose an option in each required group.";
        $("opt-error").hidden = false;
        return;
      }
      addLine(v, o);
      dialog.close();
    });
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });   // click on the backdrop
  }

  document.addEventListener("click", (e) => {
    const step = e.target.closest("button[data-line]");
    if (step) {
      const k = step.dataset.line;
      if (!cart[k] && Number(step.dataset.delta) > 0) addLine(Number(k.split(":")[0]), []);
      else change(k, Number(step.dataset.delta));
      return;
    }
    const opt = e.target.closest("button[data-options]");
    if (opt) openOptions(Number(opt.dataset.options));
  });

  // Category chips: open the category and scroll to it.
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    const target = document.getElementById(chip.dataset.target);
    if (!target) return;
    if (target.tagName === "DETAILS") target.open = true;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }));

  load();
  // "Order again": fill the cart from the earlier order, then drop ?reorder from the address.
  const re = document.getElementById("reorder-data");
  if (re) {
    try {
      cart = {};
      for (const l of JSON.parse(re.textContent)) {
        const line = { v: Number(l.v), o: (l.o || []).map(Number), q: Math.min(20, Number(l.q) || 0) };
        if (valid(line)) cart[lineKey(line.v, line.o)] = line;
      }
      save();
      history.replaceState(null, "", location.pathname);
    } catch (e) {}
  }

  render();
  // Re-open the dropdowns that already contain something from the saved order.
  for (const line of Object.values(cart)) {
    let node = document.querySelector('.ctl[data-variant="' + line.v + '"]');
    while (node && (node = node.closest("details"))) { node.open = true; node = node.parentElement; }
  }
})();
