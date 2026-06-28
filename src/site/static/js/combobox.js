// Searchable combobox — progressive enhancement over a native <select> (F2).
//
// WHY: the район dropdowns (hero single-select + filter rail multi-select) need type-to-filter
// over a long district list, but must stay usable with JS off. The HTML ships a real native
// <select> (the form control); this file builds an accessible widget IN FRONT of it and keeps
// the native element as the source of truth — so the GET <form> submits exactly the same params
// (single `district=` for the hero, one `district=` per choice for the multi filter → deep-link
// round-trips), and HTMX's `change` trigger still fires. If this file never loads, nothing breaks.
//
// A11y (WAI-ARIA combobox + listbox popup):
//   - text input role="combobox", aria-expanded, aria-controls, aria-autocomplete="list";
//   - popup role="listbox" (aria-multiselectable for multi), children role="option"
//     with aria-selected; the active row tracked via aria-activedescendant (no focus theft);
//   - keyboard: ArrowDown/Up move the active row, Enter/Space toggle/pick, Esc closes,
//     Home/End jump, type-to-filter, Backspace on an empty multi input pops the last chip;
//   - reduced-motion: no enter/exit transition when the user asks for it.

(function () {
  "use strict";

  var REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var uid = 0;

  function enhance(mount) {
    var select = mount.querySelector("select");
    if (!select || mount.dataset.comboboxReady === "1") return;
    mount.dataset.comboboxReady = "1";

    var multiple = mount.hasAttribute("data-combobox-multiple") || select.multiple;
    var placeholder = mount.getAttribute("data-combobox-placeholder") || "";
    var labelId = select.getAttribute("aria-labelledby");
    var ariaLabel = select.getAttribute("aria-label");
    var id = "cbx-" + ++uid;

    // Model: read the option list straight off the native <select> (skip the empty
    // "любой район" placeholder option — it just means "no filter", not a real choice).
    var items = [];
    Array.prototype.forEach.call(select.options, function (opt) {
      if (opt.value === "") return;
      items.push({ value: opt.value, label: opt.textContent.trim(), opt: opt });
    });

    // --- DOM ----------------------------------------------------------------------------
    var box = document.createElement("div");
    box.className = "combobox";

    var field = document.createElement("div");
    field.className = "combobox-field";

    var chips = document.createElement("div"); // selected chips (multi only)
    chips.className = "combobox-chips";

    var input = document.createElement("input");
    input.type = "text";
    input.className = "combobox-input";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("aria-controls", id + "-list");
    if (placeholder) input.placeholder = placeholder;
    if (labelId) input.setAttribute("aria-labelledby", labelId);
    else if (ariaLabel) input.setAttribute("aria-label", ariaLabel);

    var list = document.createElement("ul");
    list.className = "combobox-list";
    list.id = id + "-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    if (multiple) list.setAttribute("aria-multiselectable", "true");
    if (labelId) list.setAttribute("aria-labelledby", labelId);
    else if (ariaLabel) list.setAttribute("aria-label", ariaLabel);

    field.appendChild(chips);
    field.appendChild(input);
    box.appendChild(field);
    box.appendChild(list);

    // Keep the native <select> in the DOM (it's the submitted control) but hide it visually
    // and from AT — the combobox is the accessible surface now.
    select.classList.add("combobox-native");
    select.setAttribute("tabindex", "-1");
    select.setAttribute("aria-hidden", "true");
    mount.insertBefore(box, select);

    var open = false;
    var active = -1; // index into the currently-rendered (filtered) rows
    var rows = []; // {el, item}

    function selectedValues() {
      var out = [];
      items.forEach(function (it) {
        if (it.opt.selected) out.push(it.value);
      });
      return out;
    }

    function isSelected(it) {
      return it.opt.selected;
    }

    // Mutate the native <select>, then fire a real `change` so HTMX (hx-trigger="change")
    // re-swaps the results and filters.js syncs — single source of truth stays the <select>.
    function commit() {
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function setSelected(it, on) {
      if (multiple) {
        it.opt.selected = on;
      } else {
        items.forEach(function (other) {
          other.opt.selected = other === it && on;
        });
        // single-select: also clear when picking the placeholder-equivalent (none)
        if (!on) it.opt.selected = false;
      }
      renderChips();
      commit();
    }

    function renderChips() {
      if (!multiple) {
        var cur = items.filter(isSelected)[0];
        input.value = "";
        input.placeholder = cur ? cur.label : placeholder;
        return;
      }
      chips.textContent = "";
      items.filter(isSelected).forEach(function (it) {
        var chip = document.createElement("span");
        chip.className = "combobox-chip";
        chip.textContent = it.label;
        var x = document.createElement("button");
        x.type = "button";
        x.className = "combobox-chip-x";
        x.setAttribute("aria-label", "Убрать «" + it.label + "»");
        x.textContent = "×";
        x.addEventListener("click", function (e) {
          e.stopPropagation();
          setSelected(it, false);
          input.focus();
        });
        chip.appendChild(x);
        chips.appendChild(chip);
      });
    }

    function filterItems() {
      var q = input.value.trim().toLowerCase();
      return q ? items.filter(function (it) {
        return it.label.toLowerCase().indexOf(q) !== -1;
      }) : items.slice();
    }

    function renderList() {
      var matches = filterItems();
      list.textContent = "";
      rows = [];
      if (!matches.length) {
        var empty = document.createElement("li");
        empty.className = "combobox-empty";
        empty.textContent = "Ничего не найдено";
        list.appendChild(empty);
        active = -1;
        return;
      }
      matches.forEach(function (it, i) {
        var li = document.createElement("li");
        li.className = "combobox-option";
        li.id = id + "-opt-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", isSelected(it) ? "true" : "false");
        var name = document.createElement("span");
        name.className = "combobox-option-label";
        name.textContent = it.label;
        li.appendChild(name);
        var count = it.opt.getAttribute("data-count");
        if (count) {
          var c = document.createElement("span");
          c.className = "combobox-option-count mono";
          c.textContent = count;
          li.appendChild(c);
        }
        // mousedown (not click): fire before the input's blur so the panel stays put.
        li.addEventListener("mousedown", function (e) {
          e.preventDefault();
          choose(it);
        });
        li.addEventListener("mouseenter", function () {
          setActive(i);
        });
        list.appendChild(li);
        rows.push({ el: li, item: it });
      });
      active = rows.length ? 0 : -1;
      paintActive();
    }

    function paintActive() {
      rows.forEach(function (r, i) {
        r.el.classList.toggle("is-active", i === active);
      });
      if (active >= 0 && rows[active]) {
        input.setAttribute("aria-activedescendant", rows[active].el.id);
        var el = rows[active].el;
        if (el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function setActive(i) {
      active = i;
      paintActive();
    }

    function openPanel() {
      if (open) return;
      open = true;
      renderList();
      list.hidden = false;
      box.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
    }

    function closePanel() {
      if (!open) return;
      open = false;
      list.hidden = true;
      box.classList.remove("is-open");
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      if (multiple) input.value = ""; // clear the search query, keep chips
      else renderChips(); // restore the selected label as placeholder
    }

    function choose(it) {
      if (multiple) {
        setSelected(it, !isSelected(it));
        input.value = "";
        renderList(); // refresh aria-selected + keep panel open for more picks
        input.focus();
      } else {
        setSelected(it, true);
        closePanel();
      }
    }

    // --- events -------------------------------------------------------------------------
    input.addEventListener("focus", openPanel);
    input.addEventListener("click", openPanel);
    input.addEventListener("input", function () {
      if (!open) openPanel();
      else renderList();
    });

    input.addEventListener("keydown", function (e) {
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          if (!open) { openPanel(); return; }
          if (rows.length) setActive((active + 1) % rows.length);
          break;
        case "ArrowUp":
          e.preventDefault();
          if (!open) { openPanel(); return; }
          if (rows.length) setActive((active - 1 + rows.length) % rows.length);
          break;
        case "Home":
          if (open && rows.length) { e.preventDefault(); setActive(0); }
          break;
        case "End":
          if (open && rows.length) { e.preventDefault(); setActive(rows.length - 1); }
          break;
        case "Enter":
          if (open && active >= 0 && rows[active]) {
            e.preventDefault();
            choose(rows[active].item);
          }
          break;
        case " ":
          // Space picks only when the query is empty (otherwise it's a literal space to type).
          if (open && input.value === "" && active >= 0 && rows[active]) {
            e.preventDefault();
            choose(rows[active].item);
          }
          break;
        case "Escape":
          if (open) { e.preventDefault(); closePanel(); }
          break;
        case "Backspace":
          // Empty multi input → pop the last chip (familiar tag-input behaviour).
          if (multiple && input.value === "") {
            var sel = items.filter(isSelected);
            if (sel.length) { setSelected(sel[sel.length - 1], false); }
          }
          break;
        case "Tab":
          closePanel();
          break;
      }
    });

    // Click outside closes.
    document.addEventListener("mousedown", function (e) {
      if (open && !box.contains(e.target)) closePanel();
    });

    // Focus leaving the combobox closes the panel too (e.g. screen-reader virtual cursor or a
    // programmatic focus shift that never fires Tab/mousedown — otherwise the listbox lingers).
    box.addEventListener("focusout", function (e) {
      if (open && !box.contains(e.relatedTarget)) closePanel();
    });

    if (REDUCE) box.classList.add("reduce-motion");

    renderChips();
  }

  function init(root) {
    (root || document).querySelectorAll("[data-combobox]").forEach(enhance);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); });
  } else {
    init();
  }

  // Re-enhance after HTMX swaps the filter rail (the multi-select arrives fresh in the DOM).
  document.body.addEventListener("htmx:afterSwap", function (e) {
    init(e.target);
  });
})();
