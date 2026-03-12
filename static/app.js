document.addEventListener("DOMContentLoaded", () => {
  const active = document.querySelector("[data-auto-focus='true']");
  if (active instanceof HTMLElement) {
    active.focus();
  }
});
