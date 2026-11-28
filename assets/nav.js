// Boats N Hoes: phone menu toggle for simple subpages (no other behaviour).
(() => {
  const menu = document.getElementById('menubtn'), nav = document.getElementById('navlinks');
  if (!menu || !nav) return;
  menu.addEventListener('click', () => menu.setAttribute('aria-expanded', String(nav.classList.toggle('open'))));
  nav.querySelectorAll('a').forEach((a) => a.addEventListener('click', () => {
    nav.classList.remove('open'); menu.setAttribute('aria-expanded', 'false');
  }));
})();
