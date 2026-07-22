/* Keep the floating Telegram CTA out of the homepage hero on small screens.
 *
 * The mobile bottom navigation already exposes Telegram, so hiding the FAB while the hero is
 * visible removes an overlapping tap target without taking away the contact path.  Once the
 * visitor scrolls past the hero, the FAB resumes its site-wide sticky-CTA role.
 */
(function () {
  "use strict";

  var fab = document.querySelector(".tg-fab");
  var hero = document.querySelector(".hero");
  if (!fab || !hero || !window.IntersectionObserver) return;

  var mobile = window.matchMedia("(max-width: 620px)");
  var observer = new IntersectionObserver(function (entries) {
    var visible = entries[0] && entries[0].isIntersecting && mobile.matches;
    fab.classList.toggle("is-hero-visible", Boolean(visible));
  });

  observer.observe(hero);
  mobile.addEventListener("change", function () {
    if (!mobile.matches) fab.classList.remove("is-hero-visible");
  });
}());
