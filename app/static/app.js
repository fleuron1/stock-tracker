// The only script in the app. Kept in its own file rather than inline so the
// page can forbid inline scripts entirely -- see the Content-Security-Policy
// header, which is what makes an injected <script> inert even if one ever got
// stored in the database.
(function () {
  "use strict";

  // The autofocus attribute alone isn't dependable -- browsers skip it when a
  // page opens in a background tab -- and a scan that lands nowhere is
  // silently lost, so put the cursor in the box ourselves.
  var box = document.querySelector(".topsearch input");
  if (box && box.hasAttribute("autofocus")) { box.focus(); }

  // "/" jumps to the search box from any page, for typing rather than scanning.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.altKey || e.metaKey) { return; }
    var tag = (document.activeElement.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") { return; }
    e.preventDefault();
    if (box) { box.focus(); }
  });

  // Forms that ask before doing something hard to undo say so with
  // data-confirm, rather than an inline onsubmit handler.
  document.addEventListener("submit", function (e) {
    var question = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (question && !window.confirm(question)) { e.preventDefault(); }
  });
})();
