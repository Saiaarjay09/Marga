// Normalizes a bare directory-style URL to end in a slash, so relative
// asset paths (css/, js/, data/) resolve the same way on every host --
// a GitHub Pages project page, a custom domain, or a Tailscale path mount.
if (location.pathname.length > 1 && !location.pathname.endsWith('/') && !/\.[a-z0-9]+$/i.test(location.pathname)) {
  location.replace(location.pathname + '/' + location.search + location.hash);
}
