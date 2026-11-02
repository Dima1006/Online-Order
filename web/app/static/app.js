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
