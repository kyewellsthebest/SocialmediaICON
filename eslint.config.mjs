/* One rule, for one bug: a name used but never declared.

   `showInspect` once read `rank.parts` while `rank` was only ever declared
   inside `clipCard`. Nothing catches that until the sheet is opened in a real
   browser and a red toast says "rank is not defined". `node --check` parses
   the file happily; only scope analysis finds it. So: no-undef, and the
   browser globals the dashboard actually touches, listed by hand so a typo
   for one of them is caught too. */
const BROWSER = [
  "AbortController", "CustomEvent", "Image", "IntersectionObserver", "URL",
  "URLSearchParams", "clearInterval", "clearTimeout", "console", "document",
  "fetch", "history", "localStorage", "location", "navigator",
  "requestAnimationFrame", "setInterval", "setTimeout", "window",
];

export default [
  {
    files: ["api/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: Object.fromEntries(BROWSER.map((name) => [name, "readonly"])),
    },
    rules: { "no-undef": "error" },
  },
];
