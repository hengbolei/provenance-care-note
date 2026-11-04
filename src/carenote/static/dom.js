// Small DOM and formatting helpers shared by the page modules.
import { getLocale, t } from "/static/i18n.js";

export const labelFor = (prefix, value) => (value ? t(`${prefix}.${value}`) : "");

export function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function showFeedback(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

export function formatDate(value) {
  return new Intl.DateTimeFormat(getLocale(), {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(`${value}T00:00:00`));
}

export function formatDateTime(value) {
  return new Intl.DateTimeFormat(getLocale(), {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
