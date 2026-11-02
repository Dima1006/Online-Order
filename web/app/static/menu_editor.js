document.addEventListener("click", (e) => {
  const add = e.target.closest(".add-size");
  const remove = e.target.closest(".remove-size");
  if (add) {
    const box = add.closest(".sizes");
    const rows = box.querySelectorAll(".size-row");
    const row = rows[rows.length - 1].cloneNode(true);
    row.querySelectorAll("input").forEach((i) => (i.value = ""));
    add.before(row);
    row.querySelector("input").focus();
  } else if (remove) {
    const box = remove.closest(".sizes");
    const rows = box.querySelectorAll(".size-row");
    if (rows.length > 1) remove.closest(".size-row").remove();
    else rows[0].querySelectorAll("input").forEach((i) => (i.value = ""));
  }
});
