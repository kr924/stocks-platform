/**
 * Colour theme. Light is the default; the choice is remembered per browser.
 * Applied as <html data-theme="..."> so index.css's token sets take over, and
 * announced with a "themechange" event for the canvas chart, which cannot
 * read CSS variables and has to be rebuilt.
 */
export type Theme = "light" | "dark";

const KEY = "stocks-eye-theme";

export function getTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export function setTheme(theme: Theme) {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* private mode: the choice lasts for this page only */
  }
  applyTheme(theme);
  window.dispatchEvent(new Event("themechange"));
}
