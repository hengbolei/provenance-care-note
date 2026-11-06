// The shared public demo says what it is before anyone signs in, and an embedded page (such as a
// Hugging Face Space, which frames the app) offers to open itself in a tab, where the
// SameSite=Strict session cookie works.
import { onLanguageChange, t } from "/static/i18n.js";

const banner = document.querySelector("#demo-banner");
const text = document.querySelector("#demo-banner-text");
const openTab = document.querySelector("#demo-open-tab");

let info = null;
const framed = (() => {
  try {
    return window.self !== window.top;
  } catch {
    return true; // a cross-origin parent hides window.top: still framed
  }
})();

function render() {
  const showInfo = Boolean(info?.public_demo);
  text.hidden = !showInfo;
  if (showInfo) text.textContent = t("demo.banner", { minutes: info.reset_minutes });
  openTab.hidden = !framed;
  if (framed) {
    openTab.textContent = t("demo.openTab");
    openTab.href = window.location.href;
  }
  banner.hidden = !showInfo && !framed;
}

export async function initDemoBanner() {
  try {
    const response = await fetch("/api/demo", { credentials: "same-origin" });
    info = response.ok ? await response.json() : null;
  } catch {
    info = null;
  }
  render();
  onLanguageChange(render);
}
