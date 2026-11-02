(() => {
  const dataEl = document.getElementById("menu-data");
  if (!dataEl) return;
  const menu = JSON.parse(dataEl.textContent);
  const key = "cart:" + document.body.dataset.slug;
  const isOpen = document.body.dataset.open !== "0";
  let cart = {};
  try { cart = JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { cart = {}; }
  for (const id of Object.keys(cart)) if (!menu[id]) delete cart[id];

  const $ = (id) => document.getElementById(id);
  const money = (g) => Math.floor(g / 100) + "," + String(g % 100).padStart(2, "0") + " zł";
  const save = () => { try { localStorage.setItem(key, JSON.stringify(cart)); } catch (e) {} };

  function el(tag, props = {}, ...kids) {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...kids);
    return node;
  }
  function stepBtn(text, id, delta, label) {
    const b = el("button", { type: "button", textContent: text });
    b.dataset.variant = id; b.dataset.delta = delta;
    b.setAttribute("aria-label", label);
    return b;
  }
  const label = (id) => menu[id].name + (menu[id].label ? " (" + menu[id].label + ")" : "");

  function change(id, delta) {
    cart[id] = Math.min(20, (cart[id] || 0) + delta);
    if (cart[id] <= 0) delete cart[id];
    save();
    render();
  }

  function renderControls() {
    document.querySelectorAll(".ctl").forEach((ctl) => {
      const id = ctl.dataset.variant, qty = cart[id] || 0;
      ctl.replaceChildren();
      if (!menu[id]) return;
      if (!qty) {
        const b = stepBtn("Add", id, 1, "Add " + label(id));
        b.className = "add"; b.disabled = !isOpen;
        ctl.append(b);
      } else {
        ctl.className = "ctl stepper";
        ctl.append(stepBtn("−", id, -1, "Remove one " + label(id)), el("span", { textContent: qty }),
                   stepBtn("+", id, 1, "Add one " + label(id)));
        return;
      }
      ctl.className = "ctl";
    });
    document.querySelectorAll(".item[data-variants]").forEach((li) => {
      const badge = li.querySelector(".incart");
      if (!badge) return;
      const n = li.dataset.variants.split(",").reduce((sum, id) => sum + (cart[id] || 0), 0);
      badge.hidden = n === 0;
      badge.textContent = n + " in order";
    });
  }

  function render() {
    const lines = $("cart-lines");
    lines.replaceChildren();
    let total = 0, count = 0;
    for (const [id, qty] of Object.entries(cart)) {
      total += menu[id].price * qty;
      count += qty;
      lines.append(el("li", {},
        el("span", { textContent: label(id) }),
        el("strong", { textContent: money(menu[id].price * qty) }),
        el("span", { className: "qty" },
          stepBtn("−", id, -1, "Remove one " + label(id)), el("span", { textContent: qty }),
          stepBtn("+", id, 1, "Add one " + label(id)))));
    }
    $("cart-empty").hidden = count > 0;
    $("cart-total").textContent = money(total);
    const submit = $("submit-order"), json = $("cart-json"); // absent when the visitor is not logged in
    if (submit) submit.disabled = count === 0 || !isOpen;
    if (json) json.value = JSON.stringify(Object.entries(cart).map(([id, qty]) => ({ variant_id: Number(id), qty })));
    $("cart-bar").hidden = count === 0;
    $("cart-bar-count").textContent = count + (count === 1 ? " item" : " items");
    $("cart-bar-total").textContent = money(total);
    renderControls();
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-variant]");
    if (b) change(b.dataset.variant, Number(b.dataset.delta));
  });

  render();
  // Re-open the dropdowns that already contain something from the saved order.
  for (const id of Object.keys(cart)) {
    let node = document.querySelector('.ctl[data-variant="' + id + '"]');
    while (node && (node = node.closest("details"))) { node.open = true; node = node.parentElement; }
  }
})();
