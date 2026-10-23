// The Quote page's paste box: one big button that reads the clipboard, and a field to paste
// into by hand when the browser won't share the clipboard (it asks first on iPhones).
// It also stores the "betting unlocked" cookie when the page passes one in `data.cookie`.

export default function (component) {
  const { parentElement, setTriggerValue } = component;
  const data = component.data || {};
  if (data.cookie) document.cookie = data.cookie;

  let root = parentElement.querySelector(".paste");
  if (!root) {
    root = document.createElement("div");
    root.className = "paste";
    root.innerHTML = `
      <button class="big" data-act="paste"><span class="ic">📋</span><span class="lbl"></span></button>
      <input data-ref="field" inputmode="text" autocomplete="off" autocapitalize="characters" spellcheck="false"
             placeholder="…or paste a ticker or CapyEV link here">
      <div class="hint" data-ref="hint"></div>`;
    parentElement.appendChild(root);
    const field = root.querySelector('[data-ref="field"]');
    const hint = root.querySelector('[data-ref="hint"]');
    const send = (text) => {
      text = (text || "").trim();
      if (!text) return;
      setTriggerValue("text", text);
      field.value = "";
      hint.textContent = "";
    };
    root.querySelector('[data-act="paste"]').addEventListener("click", () => {
      const fail = () => {
        field.focus();
        hint.textContent = "Your browser didn't share the clipboard. Tap and hold the box above, then Paste.";
      };
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then((t) => (t && t.trim() ? send(t) : fail()), fail);
      } else fail();
    });
    field.addEventListener("paste", (e) => {
      const t = (e.clipboardData || window.clipboardData).getData("text");
      if (t) { e.preventDefault(); send(t); }
    });
    field.addEventListener("keydown", (e) => { if (e.key === "Enter") send(field.value); });
    field.addEventListener("change", () => send(field.value));
  }
  root.classList.toggle("small", !!data.small);
  root.querySelector(".lbl").textContent = data.label || "Paste ticker";
}
