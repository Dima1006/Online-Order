// Confirm dialogs for dangerous forms (inline onsubmit handlers are blocked by our Content-Security-Policy).
document.addEventListener("submit", (e) => {
  const message = e.target.dataset && e.target.dataset.confirm;
  if (message && !window.confirm(message)) e.preventDefault();
});

// After an order is placed, empty the saved cart of that venue.
(() => {
  const slug = document.body.dataset.clearCart;
  if (!slug) return;
  try { localStorage.removeItem("cart:" + slug); } catch (e) {}
})();

// Close open dropdown menus on outside click or Escape.
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.pop[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll("details.pop[open]").forEach((d) => { d.open = false; });
});

// Prevent double submits: disable the submit button right after the first submit.
document.addEventListener("submit", (e) => {
  if (e.defaultPrevented) return;
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
  setTimeout(() => {
    form.querySelectorAll('button:not([type="button"]), input[type="submit"]').forEach((b) => { b.disabled = true; });
  }, 0);
});
// A page restored from the back/forward cache must not keep buttons disabled.
window.addEventListener("pageshow", (e) => {
  if (e.persisted) document.querySelectorAll("form button:disabled").forEach((b) => { b.disabled = false; });
});
