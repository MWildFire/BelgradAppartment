/* C4 — share button (Web Share API + copy-link fallback).
 *
 * Progressive enhancement: the [data-share] button works as a plain element with no JS (it just
 * does nothing then) but with JS it invokes navigator.share() where available, else copies the
 * canonical /apt URL to the clipboard and shows a transient «Скопировано» confirmation. No deps.
 */
(function () {
  "use strict";

  function flash(btn, text) {
    var original = btn.getAttribute("data-share-label-original");
    if (original === null) {
      btn.setAttribute("data-share-label-original", btn.textContent);
      original = btn.textContent;
    }
    btn.textContent = text;
    window.setTimeout(function () {
      btn.textContent = original;
    }, 1800);
  }

  function copy(url, btn) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        function () {
          flash(btn, "Ссылка скопирована");
        },
        function () {
          flash(btn, url);
        }
      );
      return;
    }
    // Legacy fallback: a hidden textarea + execCommand('copy').
    try {
      var ta = document.createElement("textarea");
      ta.value = url;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      flash(btn, "Ссылка скопирована");
    } catch (e) {
      flash(btn, url);
    }
  }

  function onClick(e) {
    var btn = e.currentTarget;
    var url = btn.getAttribute("data-share-url") || window.location.href;
    var title = btn.getAttribute("data-share-title") || document.title;
    if (navigator.share) {
      navigator.share({ title: title, url: url }).catch(function () {
        /* user cancelled or share failed — silently ignore (no fallback needed) */
      });
      return;
    }
    copy(url, btn);
  }

  function wire() {
    var buttons = document.querySelectorAll("[data-share]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", onClick);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
