/* Loaded in the page head, before anything is drawn, so a dark-theme choice doesn't flash light first.
   The switch itself lives in app.js (setTheme). Light is the default. */
try {
  if (localStorage.getItem("invoice-flow-theme") === "dark") document.documentElement.dataset.theme = "dark";
} catch (e) { /* storage blocked: light it is */ }
