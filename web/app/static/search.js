(() => {
  const input = document.querySelector(".search input");
  const box = document.getElementById("results");
  if (!input || !box) return;
  let timer, seq = 0;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const mine = ++seq, q = input.value.trim();
      try {
        const res = await fetch("/?partial=1&q=" + encodeURIComponent(q));
        if (!res.ok || mine !== seq) return; // ignore out-of-date answers
        box.innerHTML = await res.text();
        history.replaceState(null, "", q ? "/?q=" + encodeURIComponent(q) : "/");
      } catch (e) {}
    }, 200);
  });
})();
