// Shared browser-geolocation request, used by any page with a "use my
// location" button. The guards are the point: without them the button reads as
// dead in two common situations that took real debugging to pin down, so
// duplicating this per page would mean re-learning both.
//
// Callers supply what to say and what to do; this owns only the asking.
function requestCoordinates({ onStatus, onCoords, fallbackAdvice }) {
  const advice = fallbackAdvice ? ` ${fallbackAdvice()}` : "";

  // Geolocation only works in a secure context: https, or a localhost origin.
  // The app binds 0.0.0.0, so reaching it by LAN address silently fails, and
  // Chrome reports that as a plain permission denial, which sends you hunting
  // through browser settings for no reason. Say what is actually wrong.
  if (window.isSecureContext === false) {
    onStatus(
      "Browsers only share a location over https or on localhost, and this "
      + `page was opened at ${window.location.hostname}. Open it at `
      + `http://localhost:${window.location.port || 80} instead.`);
    return;
  }
  if (!navigator.geolocation) {
    onStatus(`This browser can't share a location.${advice}`);
    return;
  }

  onStatus("Asking your browser for your location…");
  navigator.geolocation.getCurrentPosition(
    (position) => onCoords({
      lat: position.coords.latitude,
      lng: position.coords.longitude,
    }),
    (error) => {
      // Without an explicit timeout below, this callback may never fire at
      // all: a desktop with OS location services switched off can leave the
      // request pending indefinitely, which also reads as a dead button.
      const reason =
        error.code === error.PERMISSION_DENIED
          ? "Location sharing was blocked. Check the location permission for "
            + "this site in your browser's address bar or settings."
          : error.code === error.TIMEOUT
            ? "Your browser took too long to answer. On a desktop, check that "
              + "location services are enabled for it in your system settings."
            : "Your device couldn't determine a location.";
      onStatus(`${reason}${advice}`);
    },
    { timeout: 10000, maximumAge: 60000 },
  );
}
