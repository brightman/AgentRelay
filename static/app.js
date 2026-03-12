async function copyText(text) {
  if (!text) {
    return false;
  }
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_err) {
      // fall through to textarea fallback
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch (_err) {
    document.body.removeChild(textarea);
    return false;
  }
}

function markCopied(button) {
  const original = button.dataset.copyLabel || button.textContent || "";
  button.dataset.copyLabel = original;
  button.textContent = "Copied";
  button.classList.add("copied");
  window.setTimeout(() => {
    button.textContent = original;
    button.classList.remove("copied");
  }, 1200);
}

document.addEventListener("DOMContentLoaded", () => {
  const active = document.querySelector("[data-auto-focus='true']");
  if (active instanceof HTMLElement) {
    active.focus();
  }

  document.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    const button = target.closest("[data-copy-text]");
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const text = button.dataset.copyText || "";
    const ok = await copyText(text);
    if (ok) {
      markCopied(button);
      return;
    }
    window.prompt("Copy this text", text);
  });
});
