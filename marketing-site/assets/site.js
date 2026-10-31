// Where the PhotoGen app lives. This is the one place to change it.
// Every app link is marked up as <a data-app="/signup" href="https://photogen.ashbi.ca/signup">,
// so links work without JS and are rewritten here if APP_URL changes.
const APP_URL = 'https://photogen.ashbi.ca';

document.querySelectorAll('a[data-app]').forEach((link) => {
  link.href = APP_URL + link.dataset.app;
});
