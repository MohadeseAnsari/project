/* Panel shell behaviour: the collapsible sidebar and the tablet drawer.
 *
 * Deliberately tiny and dependency-free. htmx owns data; this owns the frame,
 * and the frame is the only thing that has to work before any page is
 * interactive — so a bug here blanks navigation on every page, which is why
 * the failure mode is "never hide a route", not "hide a route for tidiness".
 *
 * Two states, one mechanism:
 *   .collapsed  icon rail on desktop (>=821px)
 *   .drawer-open  off-canvas drawer on narrow screens (<=820px)
 *
 * Both are applied to the same #shell element, and neither removes a link from
 * the document: labels collapse to zero width, so every <a> keeps its href and
 * stays focusable.
 */
(function () {
  'use strict';

  var shell = document.getElementById('shell');
  if (!shell) return;

  var KEY = 'panel.rail';
  var NARROW = 820;

  /* Read the stored choice, but only honour it above the drawer breakpoint.
     Below 820px the sidebar is a drawer regardless, so a stale "collapsed"
     from a desktop session must not leave the tablet with no navigation. */
  function stored() {
    try { return window.localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
  }
  function remember(collapsed) {
    try { window.localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (e) { /* private mode */ }
  }

  function labelsOff() { document.body.classList.add('rail-collapsed'); }

  function paint() {
    var narrow = window.innerWidth <= NARROW;
    if (narrow) {
      shell.classList.remove('collapsed');
      document.body.classList.remove('rail-collapsed');
      return;
    }
    var collapsed = stored();
    shell.classList.toggle('collapsed', collapsed);
    document.body.classList.toggle('rail-collapsed', collapsed);
    syncButtons(collapsed);
  }

  function syncButtons(collapsed) {
    var buttons = document.querySelectorAll('[data-rail-toggle]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    }
    var label = document.querySelector('.rail-btn .rail-label');
    if (label) label.textContent = collapsed ? 'باز کردن منو' : 'جمع کردن منو';
    var icon = document.querySelector('.rail-btn .ic');
    if (icon) icon.textContent = collapsed ? '⇥' : '⇤';
  }

  function toggle(event) {
    event.preventDefault();
    if (window.innerWidth <= NARROW) {
      shell.classList.toggle('drawer-open');
      return;
    }
    var next = !shell.classList.contains('collapsed');
    shell.classList.toggle('collapsed', next);
    document.body.classList.toggle('rail-collapsed', next);
    remember(next);
    syncButtons(next);
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-rail-toggle]');
    if (trigger) { toggle(event); return; }
    if (event.target.closest('[data-close-drawer]')) {
      shell.classList.remove('drawer-open');
      return;
    }
    /* Following a link inside the drawer should close it, or the next page
       opens hidden behind the backdrop on a tablet. */
    if (event.target.closest('aside a') && shell.classList.contains('drawer-open')) {
      shell.classList.remove('drawer-open');
    }
  });

  /* Escape closes the drawer. A modal-ish overlay that traps no focus still
     owes the keyboard a way out. */
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') shell.classList.remove('drawer-open');
  });

  window.addEventListener('resize', paint);
  paint();
  labelsOff();
})();