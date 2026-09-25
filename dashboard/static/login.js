// Sign-in form. Credentials are only sent to the dashboard server, which verifies
// them and sets an HttpOnly session cookie; nothing is stored in the browser by this script.
(function () {
  const form = document.getElementById("login-form");
  const error = document.getElementById("login-error");
  const button = form.querySelector("button");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const username = form.username.value.trim();
    const password = form.password.value;
    if (!username || !password) {
      error.textContent = "Enter your username and password.";
      error.hidden = false;
      return;
    }
    button.disabled = true;
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
        cache: "no-store",
      });
      if (r.ok) {
        location.replace("/");
        return;
      }
      const data = await r.json().catch(() => ({}));
      error.textContent = data.error || `Sign-in failed (HTTP ${r.status}).`;
      error.hidden = false;
      form.password.value = "";
      form.password.focus();
    } catch {
      error.textContent = "Can't reach the console server.";
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
})();
