/* Apply the theme before painting. A host cookie remembers the choice across local ports. */
(function () {
  "use strict";
  const COOKIE = "revoker_theme";
  const SAVED = /(?:^|;\s*)revoker_theme=(light|dark)(?:;|$)/;
  const query = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    const match = document.cookie.match(SAVED);
    return match ? match[1] : null;
  }

  function current() {
    return saved() || (query && query.matches ? "dark" : "light");
  }

  function apply() {
    const theme = current();
    document.documentElement.setAttribute("data-theme", theme);
    const button = document.getElementById("theme-toggle");
    if (button) {
      const label = theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }
  }

  function choose(theme) {
    document.cookie = `${COOKIE}=${theme}; Max-Age=31536000; Path=/; SameSite=Strict`;
    apply();
  }

  apply();
  if (query && query.addEventListener) {
    query.addEventListener("change", () => { if (!saved()) apply(); });
  }
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    apply();
    button.addEventListener("click", () => choose(current() === "dark" ? "light" : "dark"));
  });
})();
